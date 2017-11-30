import os
import json
import logging
from uuid import uuid4


# We're using a fixed name here because chalice will
# configure the appropriate handlers for the logger that
# matches the app name.
LOG = logging.getLogger('jmespath-playground.storage')
MAX_BODY_SIZE = 1024 * 100
# Make disk space allowed for cache data.
MAX_DISK_USAGE = 500 * 1024 * 1024


class MaxSizeError(Exception):
    pass


class Config:
    def __init__(self, bucket, prefix='', max_body_size=MAX_BODY_SIZE):
        self.bucket = bucket
        self.prefix = prefix
        self.max_body_size = max_body_size


class Storage:
    def get(self, uuid):
        raise NotImplementedError("get")

    def put(self, data):
        raise NotImplementedError("put")


class SemiDBMCache:
    # This is a small wrapper around semidbm.
    # It's needed for two reasons:
    # 1. We store the parsed JSON values as cache data so we need to handle the
    # JSON load/dump ourself.
    #
    # 2. We have a fixed amount of disk storage to work with.  semidbm doesn't
    # support any notion of max disk space usage so this class needs to manage
    # that.  The approach taken here is to simply turn off caching once
    # the max disk space limit is reached.  This isn't the greatest idea, but
    # we're betting that it's unlikely we'll reach the max disk usage
    # before the function is shut down.  It's worth investigating a proper
    # eviction strategy in the future.

    def __init__(self, dbdir, check_frequency=20, max_filesize=MAX_DISK_USAGE):
        import semidbm
        self._db = semidbm.open(dbdir, 'c')
        self._max_filesize = max_filesize
        # How frequently we check the file size of the cache.
        # If we check every 20 writes, then at worst case we overshoot
        # the max size by MAX_BODY_SIZE * check_frequency, or
        # about 20MB if we use the default values for everything.
        self._check_frequency = check_frequency
        self._counter = 0
        # When we reach the max disk size, we disable
        # writing data to the cache.
        self._writes_enabled = True

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __getitem__(self, key):
        d = self._db[key].decode('utf-8')
        return json.loads(d)

    def __contains__(self, key):
        return key in self._db

    def __setitem__(self, key, value):
        if not self._writes_enabled:
            return
        v = json.dumps(value).encode('utf-8')
        self._db[key] = v
        self._counter += 1
        if self._counter >= self._check_frequency:
            self._check_max_size_reached()
            self._counter = 0

    def _check_max_size_reached(self):
        # There's no public interface for getting the
        # filename of the db so we have to use an internal
        # attribute to access the filename.
        filesize = os.path.getsize(self._db._data_filename)
        LOG.debug('SemiDBMCache filesize: %s', filesize)
        if filesize > self._max_filesize:
            LOG.debug("SemiDBMCache filesize (%s) exceeded %s, "
                      "disabling writes to cache.", filesize,
                      self._max_filesize)
            self._writes_enabled = False


class CachingStorage(Storage):
    """Wraps a storage object with a disk cache."""

    def __init__(self, real_storage, cache):
        self._real_storage = real_storage
        self._cache = cache

    def get(self, uuid):
        cached = self._cache.get(uuid)
        if cached is not None:
            LOG.debug("cache hit for %s", uuid)
            return cached
        LOG.debug("cache miss for %s, retrieving from source.", uuid)
        result = self._real_storage.get(uuid)
        self._cache[uuid] = result
        return result

    def put(self, data):
        uuid = self._real_storage.put(data)
        self._cache[uuid] = data
        return uuid


class S3Storage(Storage):
    def __init__(self, client, config):
        self._config = config
        self._client = client

    def get(self, uuid):
        bucket = self._config.bucket
        key = self._create_s3_key(uuid)
        contents = self._client.get_object(
            Bucket=bucket, Key=key)['Body'].read()
        return json.loads(contents)

    def put(self, data):
        bucket = self._config.bucket
        uuid = str(uuid4())
        key = self._create_s3_key(uuid)
        body = json.dumps(data, separators=(',', ':'))
        if len(body) > self._config.max_body_size:
            raise MaxSizeError("Request body is too large (%s), "
                               "must be less than %s bytes." % (
                                   len(body), self._config.max_body_size))
        self._client.put_object(Bucket=bucket, Key=key, Body=body)
        return uuid

    def _create_s3_key(self, uuid):
        prefix = self._config.prefix
        if not prefix:
            return uuid
        elif prefix.endswith('/'):
            prefix = prefix[:-1]
        return '%s/%s' % (prefix, uuid)
