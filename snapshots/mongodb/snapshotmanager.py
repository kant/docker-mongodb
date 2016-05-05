import sys
import urllib2
import pytz
import logging
import logging.handlers
from datetime import timedelta, datetime, tzinfo
from os import environ
from subprocess import Popen
import boto3
import botocore
from retrying import retry

class Snapshot():
    def __init__(self, snapshot_id, start_time):
        self.snapshot_id = snapshot_id
        self.start_time = start_time

class SnapshotManager():
    """class for making snapshots"""
    ## Log to stdout
    logger = logging.getLogger('Mongo Snapshot Log')
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    datetime_format = "%Y-%m-%dT%H:%M:%S.%fZ"
    sub_hourly_snapshots_minutes = 180

    def __init__(self, cluster_name, statsd=None):
        self.ec2 = self._get_ec2_client()
        self.data_device = environ.get('MONGODB_DATA_DEVICE', '/dev/xvdc')
        self.instance_id = environ.get('INSTANCE_ID')
        self.cluster_name = cluster_name
        self.statsd = statsd

    def _get_ec2_client(self):
        kwargs = {
            'region_name': environ.get('AWS_REGION', 'us-east-1'),
        }

        aws_access_key_id = environ.get('AWS_ACCESS_KEY_ID')
        aws_secret_access_key = environ.get('AWS_SECRET_ACCESS_KEY')

        if aws_access_key_id and aws_secret_access_key:
            kwargs['aws_access_key_id'] = aws_access_key_id
            kwargs['aws_secret_access_key'] = aws_secret_access_key

        return boto3.client('ec2', **kwargs)

    def get_sorted_snapshots(self):
        snapshots = self.get_snapshots()
        return sorted(snapshots, key=lambda snapshot: snapshot.start_time)

    def get_snapshots(self):
        snapshots = []
        response = self._ec2_describe_snapshots(Filters=[{"Name": "tag:ClusterName", "Values": [self.cluster_name]}] )
        if 'Snapshots' in response:
            for snapshot in response['Snapshots']:
                localized_start_time = pytz.timezone('UTC').localize(snapshot['StartTime'])
                snapshots.append(Snapshot(snapshot['SnapshotId'], localized_start_time))
        return snapshots


    def delete_snapshot(self, snapshot_id):
        self.ec2.delete_snapshot(SnapshotId=snapshot_id)

    def remove_old_snapshots(self, now, hourly_snapshots, daily_snapshots):
        #needs to sort by time ASCENDING for the rest of the code to work
        snapshots = self.get_sorted_snapshots()
        self._record_backup_metrics(now, snapshots)

        snapshots = self._remove_sub_hourly_snapshots(now, snapshots)
        snapshots = self._remove_hourly_snapshots(now, hourly_snapshots, snapshots)
        snapshots = self._remove_daily_snapshots(now, daily_snapshots, snapshots)

        # deleting the remaining snapshots
        for snapshot in snapshots:
            self.logger.info("Deleting snapshot %s %s" % (snapshot.snapshot_id, snapshot.start_time))
            self.delete_snapshot(snapshot.snapshot_id)

    def _remove_daily_snapshots(self, now, daily_snapshots, snapshots):
        keep_since_time = now - timedelta(days=daily_snapshots)
        return self._remove_bucketed_snapshots(keep_since_time, "%Y%m%d", snapshots)

    def _remove_hourly_snapshots(self, now, hourly_snapshots, snapshots):
        keep_since_time = now - timedelta(hours=hourly_snapshots)
        return self._remove_bucketed_snapshots(keep_since_time, "%Y%m%d%H", snapshots)

    def _remove_bucketed_snapshots(self, keep_since_time, bucket_key_datetime_format, snapshots):
        snapshots_to_delete = []
        snapshot_bucket = {}
        for snapshot in snapshots:
            snapshot_start = snapshot.start_time
            if snapshot_start < keep_since_time:
                snapshots_to_delete.append(snapshot)
            else:
                bucket_key = snapshot_start.strftime(bucket_key_datetime_format)
                if bucket_key in snapshot_bucket:
                    # we already have a backup for this key so we can delete this one
                    snapshots_to_delete.append(snapshot)
                else:
                    # save the first snapshot for the hour; this code assumes that the snapshots are sorted ascending already
                    snapshot_bucket[bucket_key] = snapshot

        return snapshots_to_delete

    def _remove_sub_hourly_snapshots(self, now, snapshots):
        snapshots_to_delete = []
        keep_since_time = now - timedelta(minutes=self.sub_hourly_snapshots_minutes)
        for snapshot in snapshots:
            snapshot_start = snapshot.start_time
            if snapshot_start < keep_since_time:
                snapshots_to_delete.append(snapshot)

        return snapshots_to_delete

    # records the number of missing snapshots in the last hour
    def _record_backup_metrics(self, now, snapshots):
        # since we are taking snapshots every 10 minutes there should be at least 5 backups in the last hour
        expected_snapshots = 5
        last_hour = now - timedelta(minutes=60)
        snapshot_count = 0
        for snapshot in snapshots:
            snapshot_start = snapshot.start_time
            if snapshot_start >= last_hour:
                snapshot_count += 1

        missing_snapshots = expected_snapshots - snapshot_count
        if missing_snapshots < 0:
            missing_snapshots = 0
        self.logger.info("Recording mongodb.backups.missing %s" % missing_snapshots)
        if self.statsd:
            self.statsd.gauge('mongodb.backups.missing', missing_snapshots)

    def _is_retryable_exception(exception):
        return not isinstance(exception, botocore.exceptions.ClientError)

    def create_snapshot_for_volume(self, volume_id):
        snap_name = self.cluster_name + "." + datetime.now().strftime( '%Y%m%d%H%M' )
        tags = [{ 'Key': 'ClusterName', 'Value': self.cluster_name },
                { 'Key': 'Name', 'Value' : snap_name}]
        snapshot_id = self._ec2_create_snapshot( VolumeId=volume_id, Description=snap_name )
        if not snapshot_id:
            raise SnapshotManagerException("No snapshot id found after creating snapshot")

        self._ec2_create_tags(Resources=[snapshot_id], Tags=tags)
        self.logger.info("Snapshot " + snap_name + " created.")

    @retry(retry_on_exception=_is_retryable_exception, stop_max_delay=10000, wait_exponential_multiplier=500, wait_exponential_max=2000)
    def _ec2_create_tags(self, **kwargs):
        return self.ec2.create_tags(**kwargs)

    @retry(retry_on_exception=_is_retryable_exception, stop_max_delay=10000, wait_exponential_multiplier=500, wait_exponential_max=2000)
    def _ec2_create_snapshot(self, **kwargs):
        response = self.ec2.create_snapshot(**kwargs)
        if 'SnapshotId' in response:
            return response['SnapshotId']

    def create_snapshot_for_instance(self, instance_id):
        try:
            ## Create snapshots of data volumes attached to 'server' and with block dev 'xvdc'
            ## This should only return one volume.
            volumes = self._ec2_describe_volumes(Filters=[{'Name': 'attachment.instance-id', 'Values': [instance_id]},
                                                         {'Name': 'attachment.device', 'Values': [self.data_device]}])
            for volume in volumes:
                volume_id = volume['VolumeId']
                self.logger.debug("Creating snapshot for volume " + str(volume_id) + " from instance " + instance_id)
                self.create_snapshot_for_volume(volume_id)
            else:
                self.logger.error("No applicable volumes found. Does the MongoDB instance have a block device at %s?" % self.data_device)
                return False

            return True
        except Exception as ex:
            self.logger.error("failed to create snapshot %s" % ex)
            return False

    @retry(retry_on_exception=_is_retryable_exception, stop_max_delay=10000, wait_exponential_multiplier=500, wait_exponential_max=2000)
    def _ec2_describe_volumes(self, **kwargs):
        volumes = []
        response = self.ec2.describe_volumes(**kwargs)
        if 'Volumes' in response:
            volumes = response['Volumes']
        return volumes

    def create_snapshot(self):
        if not self.instance_id:
            self.instance_id = urllib2.urlopen("http://169.254.169.254/latest/meta-data/instance-id").read()
        if not self.instance_id:
            raise SnapshotManagerException("No instance id could be found using either INSTANCE_ID environment variable or instance metadata")
        self.create_snapshot_for_instance(self.instance_id)

class SnapshotManagerException(Exception):
    def __init__(self,*args,**kwargs):
        Exception.__init__(self,*args,**kwargs)
