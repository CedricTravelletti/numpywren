from numpywren.matrix import BigMatrix, BigSymmetricMatrix
from numpywren import matrix_utils, uops
from numpywren import lambdapack as lp
from numpywren import job_runner
from numpywren.matrix_init import shard_matrix
import numpywren.wait
import pytest
import numpy as np
from numpy.linalg import cholesky
import pywren
import unittest
import concurrent.futures as fs
import time
import os
import boto3

class CholeskyTest(unittest.TestCase):
    def test_cholesky_single(self):
        X = np.random.randn(4,4)
        A = X.dot(X.T) + np.eye(X.shape[0])
        y = np.random.randn(16)
        pwex = pywren.default_executor()
        A_sharded= BigMatrix("cholesky_test_A", shape=A.shape, shard_sizes=A.shape, write_header=True)
        A_sharded.free()
        shard_matrix(A_sharded, A)
        instructions,L_sharded,trailing = lp._chol(A_sharded)
        executor = pywren.lambda_executor
        config = pwex.config
        program = lp.LambdaPackProgram(instructions, executor=executor, pywren_config=config)
        program.start()
        job_runner.lambdapack_run(program)
        program.wait()
        program.free()
        print("Program status")
        print(program.program_status())


    def test_cholesky_multi(self):
        print("RUNNING MULTI")
        np.random.seed(1)
        size = 128
        shard_size = 32
        np.random.seed(1)
        print("Generating X")
        X = np.random.randn(size, 128)
        print("Generating A")
        A = X.dot(X.T) + np.eye(X.shape[0])
        shard_sizes = (shard_size, shard_size)
        A_sharded= BigMatrix("cholesky_test_A", shape=A.shape, shard_sizes=shard_sizes, write_header=True)
        A_sharded.free()
        shard_matrix(A_sharded, A)
        instructions,L_sharded,trailing = lp._chol(A_sharded)
        pwex = pywren.default_executor()
        executor = pywren.lambda_executor
        config = pwex.config
        program = lp.LambdaPackProgram(instructions, executor=executor, pywren_config=config)
        program.start()
        #job_runner.main(program, program.queue_url)
        job_runner.lambdapack_run(program, pipeline_width=1)
        program.wait()
        print("Program status")
        print(program.program_status())
        program.free()
        profiled_blocks = program.get_all_profiling_info()
        print(lp.perf_profile(profiled_blocks))
        for pc,profiled_block in enumerate(profiled_blocks):
            total_time = 0
            actual_time = profiled_block.end_time - profiled_block.start_time
            for instr in profiled_block.instrs:
                total_time += instr.end_time - instr.start_time
            print("Block {0} total_time {1} pipelined time {2}".format(pc, total_time, actual_time))
        L_npw = L_sharded.numpy()
        L = np.linalg.cholesky(A)
        print(L_npw)
        print(L)
        assert(np.allclose(L_npw, L))





    def test_cholesky_lambda_single(self): 
        print("RUNNING single lambda")
        np.random.seed(1)
        size = 128
        shard_size = 128
        num_cores = 1
        pwex = pywren.default_executor()
        np.random.seed(1)
        print("Generating X")
        X = np.random.randn(size, 128)
        print("Generating A")
        A = X.dot(X.T) + np.eye(X.shape[0])
        shard_sizes = (shard_size, shard_size)
        A_sharded= BigMatrix("cholesky_test_A", shape=A.shape, shard_sizes=shard_sizes, write_header=True)
        A_sharded.free()
        shard_matrix(A_sharded, A)
        instructions,L_sharded,trailing = lp._chol(A_sharded)
        executor = pywren.lambda_executor
        config = pwex.config
        program = lp.LambdaPackProgram(instructions, executor=executor, pywren_config=config)
        print(program)
        program.start()
        num_cores = 1
        print("Mapping...")
        futures = pwex.map(lambda x: job_runner.lambdapack_run(program), range(num_cores), exclude_modules=["site-packages"], extra_env={"REDIS_IP": os.environ.get("REDIS_IP", "")})
        futures[0].result()
        print("Waiting...")
        pywren.wait(futures)
        [f.result() for f in futures]
        program.wait()
        print("Program status")
        print(program.program_status())
        program.free()
        profiled_blocks = program.get_all_profiling_info()
        print(lp.perf_profile(profiled_blocks))
        for pc,profiled_block in enumerate(profiled_blocks):
            total_time = 0
            actual_time = profiled_block.end_time - profiled_block.start_time
            for instr in profiled_block.instrs:
                total_time += instr.end_time - instr.start_time
            print("Block {0} total_time {1} end to end time {2}".format(pc, total_time, actual_time))
        L_npw = L_sharded.numpy()
        L = np.linalg.cholesky(A)
        print(L_npw)
        print(L)
        assert(np.allclose(L_npw, L))

    def test_cholesky_multi_process(self):
        print("RUNNING many process")
        np.random.seed(1)
        size = 16384 
        shard_size = 4096
        np.random.seed(1)
        print("Generating X")
        X = np.random.randn(size, 128)
        print("Generating A")
        A = X.dot(X.T) + np.eye(X.shape[0])
        shard_sizes = (shard_size, shard_size)
        A_sharded= BigMatrix("cholesky_test_A", shape=A.shape, shard_sizes=shard_sizes, write_header=True)
        print(A_sharded.key)
        A_sharded.free()
        print("sharding A..")
        #shard_matrix(A_sharded, A)
        instructions,L_sharded,trailing = lp._chol(A_sharded)
        pwex = pywren.default_executor()
        executor = pywren.lambda_executor
        config = pwex.config
        program = lp.LambdaPackProgram(instructions, executor=executor, pywren_config=config)
        print(program)
        program.start()
        num_cores = 16
        #pwex = lp.LocalExecutor(procs=100)
        executor = fs.ProcessPoolExecutor(num_cores)
        all_futures  = []
        for i in range(num_cores):
            all_futures.append(executor.submit(job_runner.lambdapack_run, program, pipeline_width=1))
        program.wait()
        #pywren.wait(all_futures)
        [f.result() for f in  all_futures]
        print("Program status")
        print(program.program_status())
        program.free()
        profiled_blocks = program.get_all_profiling_info()
        print(lp.perf_profile(profiled_blocks))
        for pc,profiled_block in enumerate(profiled_blocks):
            total_time = 0
            actual_time = profiled_block.end_time - profiled_block.start_time
            for instr in profiled_block.instrs:
                total_time += instr.end_time - instr.start_time
            print("Block {0} total_time {1} end to end time {2}".format(pc, total_time, actual_time))
        L_npw = L_sharded.numpy()
        L = np.linalg.cholesky(A)
        print(L_npw)
        print(L)
        assert(np.allclose(L_npw, L))

    def test_cholesky_multi_lambda(self):
        print("RUNNING many lambda")
        np.random.seed(1)
        size = 128 
        shard_size = 32
        np.random.seed(1)
        print("Generating X")
        X = np.random.randn(size, 1)
        print("Generating A")
        A = X.dot(X.T) + np.eye(X.shape[0])
        shard_sizes = (shard_size, shard_size)
        A_sharded= BigMatrix("cholesky_test_A", shape=A.shape, shard_sizes=shard_sizes, write_header=True)
        A_sharded.free()
        print("sharding....")
        shard_matrix(A_sharded, A)
        instructions,L_sharded,trailing = lp._chol(A_sharded)
        L_sharded.free()
        pwex = pywren.default_executor()
        executor = pywren.lambda_executor
        config = pwex.config
        program = lp.LambdaPackProgram(instructions, executor=executor, pywren_config=config)
        print(program)
        program.start()
        num_cores = 100
        all_futures = pwex.map(lambda x: job_runner.lambdapack_run(program), range(num_cores), exclude_modules=["site-packages"], extra_env={"REDIS_IP": os.environ.get("REDIS_IP", "")})
        program.wait()
        print("Program status")
        print(program.program_status())
        print(program.hash)
        program.free()
        L_npw = L_sharded.numpy()
        L = np.linalg.cholesky(A)
        assert(np.allclose(L_npw, L))
        profiled_blocks = program.get_all_profiling_info()
        print(lp.perf_profile(profiled_blocks))
        for pc,profiled_block in enumerate(profiled_blocks):
            total_time = 0
            actual_time = profiled_block.end_time - profiled_block.start_time
            for instr in profiled_block.instrs:
                if (instr.end_time == None or instr.start_time == None): continue
                total_time += instr.end_time - instr.start_time
            print("Instruction Block {0} operation_time {1} end_to_end time {2}".format(pc, total_time, actual_time))

    def test_cholesky_multi_standalone(self):
        print("RUNNING many standalone")
        np.random.seed(1)
        size = 128
        shard_size = 32
        np.random.seed(1)
        print("Generating X")
        X = np.random.randn(size, 128)
        print("Generating A")
        A = X.dot(X.T) + np.eye(X.shape[0])
        shard_sizes = (shard_size, shard_size)
        A_sharded= BigMatrix("cholesky_test_A", shape=A.shape, shard_sizes=shard_sizes, write_header=True)
        A_sharded.free()
        shard_matrix(A_sharded, A)
        instructions,L_sharded,trailing = lp._chol(A_sharded)
        pwex = pywren.standalone_executor()
        executor = pywren.standalone_executor
        config = pwex.config
        program = lp.LambdaPackProgram(instructions, executor=executor, pywren_config=config)
        print(program)
        program.start()
        num_cores = 1
        all_futures = pwex.map(lambda x: job_runner.lambdapack_run(program), range(num_cores))
        program.wait()
        print("Program status")
        print(program.program_status())
        program.free()
        profiled_blocks = program.get_all_profiling_info()
        print(lp.perf_profile(profiled_blocks))
        for pc,profiled_block in enumerate(profiled_blocks):
            total_time = 0
            actual_time = profiled_block.end_time - profiled_block.start_time
            for instr in profiled_block.instrs:
                total_time += instr.end_time - instr.start_time
            print("Instruction Block {0} operation_time {1} end_to_end time {2}".format(pc, total_time, actual_time))
        L_npw = L_sharded.numpy()
        L = np.linalg.cholesky(A)
        print(L_npw)
        print(L)
        assert(np.allclose(L_npw, L))





if __name__ == "__main__":
    tests = LambdapackExecutorTest()
    tests.test_cholesky_multi()
