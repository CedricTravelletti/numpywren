import concurrent.futures as fs
import io
import itertools
import os
import time

import boto3
import cloudpickle
import numpy as np
import json
import logging
from . import matrix_utils
from .matrix_utils import list_all_keys, block_key_to_block, get_local_matrix, key_exists
import pywren.wrenconfig as wc
import botocore

logger = logging.getLogger(__name__)
try:
    DEFAULT_BUCKET = wc.default()['s3']['bucket']
except Exception as e:
    DEFAULT_BUCKET = ""

class BigMatrix(object):
    def __init__(self, key,
                 shape=None,
                 shard_sizes=[],
                 bucket=DEFAULT_BUCKET,
                 prefix='numpywren.objects/',
                 dtype=np.float64,
                 transposed=False,
                 parent_fn=None,
                 write_header=False):

        if bucket is None:
            bucket = os.environ.get('PYWREN_LINALG_BUCKET')
            if bucket is None:
                raise Exception("bucket not provided and environment variable \
                        PYWREN_LINALG_BUCKET not provided")
        self.bucket = bucket
        self.prefix = prefix
        self.key = key
        self.key_base = prefix + self.key + "/"
        self.dtype = dtype
        self.transposed = transposed
        self.symmetric = False
        self.parent_fn = parent_fn
        header = self.__read_header__()
        if header is None and shape is None:
            raise Exception("header doesn't exist and no shape provided")
        if not (header is None) and shape is None:
            self.shard_sizes = header['shard_sizes']
            self.shape = header['shape']
            self.dtype = header['dtype']
        else:
            self.shape = shape
            self.shard_sizes = shard_sizes
            self.dtype = dtype

        if (len(self.shape) != len(self.shard_sizes)):
            raise Exception("shard_sizes should be same length as shape")

        self.symmetric = False
        if (write_header):
            # write a header if you want to load this value later
            self.__write_header__()

    def __write_header__(self):
        key = self.key_base + "header"
        client = boto3.client('s3')
        header = {}
        header['shape'] = self.shape
        header['shard_sizes'] = self.shard_sizes
        header['dtype'] = str(self.dtype)
        client.put_object(Key=key, Bucket = self.bucket, Body=json.dumps(header), ACL="bucket-owner-full-control")
        return 0

    @property
    def T(self):
        return self.__transpose__()

    def __str__(self):
        rep = "{0}({1})".format(self.__class__.__name__, self.key)
        if (self.transposed):
            rep += ".T"
        return rep

    def __transpose__(self):
        transposed = self.__class__(key=self.key,
                                   shape=self.shape[::-1],
                                   shard_sizes=self.shard_sizes[::-1],
                                   bucket=self.bucket,
                                   prefix=self.prefix,
                                   dtype=self.dtype,
                                   transposed=True,
                                   parent_fn=self.parent_fn)
        return transposed




    @property
    def blocks_exist(self):
        prefix = self.prefix + self.key
        all_keys = list_all_keys(self.bucket, prefix)
        return list(filter(lambda x: x != None, map(block_key_to_block, all_keys)))

    @property
    def blocks(self):
        return self._blocks()

    @property
    def block_idxs_exist(self):
        all_block_idxs = self.block_idxs
        all_blocks = self.blocks
        blocks_exist = set(self.blocks_exist)
        block_idxs_exist = []
        for i, block in enumerate(all_blocks):
            if block in blocks_exist:
                block_idxs_exist.append(all_block_idxs[i])
        return block_idxs_exist

    @property
    def blocks_not_exist(self):
        blocks = set(self.blocks)
        block_exist = set(self.blocks_exist)
        return list(filter(lambda x: x, list(block_exist.symmetric_difference(blocks))))

    @property
    def block_idxs_not_exist(self):
        block_idxs = set(self.block_idxs)
        block_idxs_exist = set(self.block_idxs_exist)
        return list(filter(lambda x: x, list(block_idxs_exist.symmetric_difference(block_idxs))))

    @property
    def block_idxs(self):
        return self._block_idxs()

    def _blocks(self, axis=None):
        all_blocks = []
        for i in range(len(self.shape)):
            blocks_axis = [(j, j + self.shard_sizes[i]) for j in range(0, self.shape[i], self.shard_sizes[i])]
            if blocks_axis[-1][1] > self.shape[i]:
                blocks_axis.pop()

            if blocks_axis[-1][1] < self.shape[i]:
                blocks_axis.append((blocks_axis[-1][1], self.shape[i]))
            all_blocks.append(blocks_axis)

        if axis is None:
            return list(itertools.product(*all_blocks))
        elif (type(axis) != int):
            raise Exception("Axis must be integer")
        else:
            return all_blocks[axis]

    def _block_idxs(self, axis=None):
        idxs = [list(range(len(self._blocks(axis=i)))) for i in range(len(self.shape))]
        if axis is None:
            return list(itertools.product(*idxs))
        elif (type(axis) != int):
            raise Exception("Axis must be integer")
        else:
            return idxs[axis]

    def __get_matrix_shard_key__(self, real_idxs):
            key_string = ""

            shard_sizes = self.shard_sizes
            if (self.transposed):
                shard_sizes = reversed(shard_sizes)
                real_idxs = reversed(real_idxs)
            for ((sidx, eidx), shard_size) in zip(real_idxs, shard_sizes):
                key_string += "{0}_{1}_{2}_".format(sidx, eidx, shard_size)

            return self.key_base + key_string

    def __read_header__(self):
        client = boto3.client('s3')
        try:
            key = self.key_base + "header"
            header = json.loads(client.get_object(Bucket=self.bucket, Key=key)['Body'].read())
        except:
            header = None
        return header

    def __delete_header__(self):
        key = self.key_base + "header"
        client = boto3.client('s3')
        client.delete_object(Bucket=self.bucket, Key=key)


    def __block_idx_to_real_idx__(self, block_idx):
        starts = []
        ends = []
        for i in range(len(self.shape)):
            start = block_idx[i]*self.shard_sizes[i]
            end = min(start+self.shard_sizes[i], self.shape[i])
            starts.append(start)
            ends.append(end)
        return tuple(zip(starts, ends))


    def __shard_idx_to_key__(self, block_idx):

        real_idxs = self.__block_idx_to_real_idx__(block_idx)
        key = self.__get_matrix_shard_key__(real_idxs)
        return key

    def __s3_key_to_byte_io__(self, key):
        n_tries = 0
        max_n_tries = 5
        bio = None
        client = boto3.client('s3')
        while bio is None and n_tries <= max_n_tries:
            try:
                bio = io.BytesIO(client.get_object(Bucket=self.bucket, Key=key)['Body'].read())
            except Exception as e:
                raise
                n_tries += 1
        if bio is None:
            raise Exception("S3 Read Failed")
        return bio

    def __save_matrix_to_s3__(self, X, out_key, client=None):
        if (client == None):
            client = boto3.client('s3')
        outb = io.BytesIO()
        np.save(outb, X)
        response = client.put_object(Key=out_key, Bucket=self.bucket, Body=outb.getvalue(),ACL="bucket-owner-full-control")
        return response

    def _register_parent(self, parent_fn):
        self.parent_fn = parent_fn

    def get_block(self, *block_idx):
        if (len(block_idx) != len(self.shape)):
            raise Exception("Get block query does not match shape")
        key = self.__shard_idx_to_key__(block_idx)
        exists = key_exists(self.bucket, key)
        if (not exists and self.parent_fn == None):
            print(self.bucket)
            print(key)
            raise Exception("Key does not exist, and no parent function prescripted")
        elif (not exists and self.parent_fn != None):
            X_block = self.parent_fn(self, *block_idx)
        else:
            bio = self.__s3_key_to_byte_io__(key)
            X_block = np.load(bio)
        if (self.transposed):
            X_block = X_block.T
        return X_block

    def put_block(self, block, *block_idx):
        real_idxs = self.__block_idx_to_real_idx__(block_idx)
        current_shape = tuple([e - s for s,e in real_idxs])

        if (block.shape != current_shape):
            raise Exception("Incompatible block size: {0} vs {1}".format(block.shape, current_shape))
        if (self.transposed):
            block = block.T
        key = self.__shard_idx_to_key__(block_idx)
        return self.__save_matrix_to_s3__(block, key)


    def delete_block(self, *block_idx):
        key = self.__shard_idx_to_key__(block_idx)
        client = boto3.client('s3')
        return client.delete_object(Key=key, Bucket=self.bucket)

    def free(self):
        [self.delete_block(*x) for x in self.block_idxs_exist]
        return 0

    def delete(self):
        self.free()
        self.__delete_header__()
        return 0


    def numpy(self, workers=16):
        return matrix_utils.get_local_matrix(self, workers)

class Scalar(BigMatrix):
    def __init__(self, key,
                 bucket=DEFAULT_BUCKET,
                 prefix='numpywren.objects/',
                 parent_fn=None, 
                 dtype='float64'):
        self.bucket = bucket
        self.prefix = prefix
        self.key = key
        self.key_base = prefix + self.key + "/"
        self.dtype = dtype
        self.transposed = False
        self.symmetric = True
        self.parent_fn = parent_fn
        self.shard_sizes = [1]
        self.shape = [1]

    def numpy(self, workers=1):
        return BigMatrix.get_block(self, 0)[0]

    def get(self, workers=1):
        return BigMatrix.get_block(self, 0)[0]

    def put(self, value):
        value = np.array([value])
        BigMatrix.put_block(self, value, 0)

    def __str__(self):
        rep = "Scalar({0})".format(self.key)
        return rep




class BigSymmetricMatrix(BigMatrix):

    def __init__(self, key,
                 shape=None,
                 shard_sizes=[],
                 bucket=DEFAULT_BUCKET,
                 prefix='numpywren.objects/',
                 dtype=np.float64,
                 parent_fn=None,
                 write_header=False):
        BigMatrix.__init__(self, key, shape, shard_sizes, bucket, prefix, dtype, parent_fn, write_header)
        self.symmetric = True

    @property
    def T(self):
        return self


    def _symmetrize_idx(self, block_idx):
        if np.all(block_idx[0] > block_idx[-1]):
            return tuple(block_idx)
        else:
            return tuple(reversed(block_idx))

    def _symmetrize_all_idxs(self, all_block_idxs):
        return sorted(list(set((map(lambda x: tuple(self._symmetrize_idx(x)), all_block_idxs)))))

    def _blocks(self, axis=None):
        if axis is None:
            block_idxs = self._block_idxs()
            blocks = [self.__block_idx_to_real_idx__(x) for x in block_idxs]
            return blocks
        elif (type(axis) != int):
            raise Exception("Axis must be integer")
        else:
            return super()._blocks(axis=axis)

    def _block_idxs(self, axis=None):
        all_block_idxs = super()._block_idxs(axis=axis)
        if (axis == None):
            valid_block_idxs = self._symmetrize_all_idxs(all_block_idxs)
            return valid_block_idxs
        else:
            return all_block_idxs

    def get_block(self, *block_idx):
        # For symmetric matrices it suffices to only read from lower triangular
        flipped = False
        block_idx_sym = self._symmetrize_idx(block_idx)
        if block_idx_sym != block_idx:
            flipped = True
        key = self.__shard_idx_to_key__(block_idx_sym)
        exists = key_exists(self.bucket, key)
        if (not exists and self.parent_fn == None):
            raise Exception("Key does not exist, and no parent function prescripted")
        elif (not exists and self.parent_fn != None):
            X_block = self.parent_fn(self, *block_idx_sym)
        else:
            bio = self.__s3_key_to_byte_io__(key)
            X_block = np.load(bio)
        if (flipped):
            X_block = X_block.T
        return X_block

    def put_block(self, block, *block_idx):
        block_idx_sym = self._symmetrize_idx(block_idx)
        if block_idx_sym != block_idx:
            flipped = True
            block = block.T
        real_idxs = self.__block_idx_to_real_idx__(block_idx_sym)
        current_shape = tuple([e - s for s,e in real_idxs])
        if (block.shape != current_shape):
            raise Exception("Incompatible block size: {0} vs {1}".format(block.shape, current_shape))
        key = self.__shard_idx_to_key__(block_idx)
        return self.__save_matrix_to_s3__(block, key)


    def delete_block(self, *block_idx):
        client = boto3.client('s3')
        block_idx_sym = self._symmetrize_idx(block_idx)
        if block_idx_sym != block_idx:
            flipped = True
        key = self.__shard_idx_to_key__(block_idx_sym)
        client = boto3.client('s3')
        return client.delete_object(Key=key, Bucket=self.bucket)


