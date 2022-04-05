# -*- coding: utf-8 -*-
"""
Created on Mon Apr  4 20:15:01 2022

@author: johan
"""

import shutil
import random
import shlex
from pathlib import Path
import re


import sys
from contextlib import contextmanager
from types import MethodType
import time
from functools import wraps

from fairseq_cli import train

# =============================================================================
# from muss.utils.helpers import (
#     log_std_streams,
#     # lock_directory,
#     # create_directory_or_skip,
#     # yield_lines,
#     # write_lines,
#     mock_cli_args,
#     # create_temp_dir,
#     # mute,
#     args_dict_to_str,
#     print_running_time,
# )
# =============================================================================

from muss.text import remove_multiple_whitespaces
from muss.utils.training import clear_cuda_cache

from muss.fairseq.main import prepare_exp_dir

from muss.mining.training import get_mbart_kwargs
# %%


@contextmanager
def redirect_streams(source_streams, target_streams):
    # We assign these functions before hand in case a target stream is also a source stream.
    # If it's the case then the write function would be patched leading to infinie recursion
    target_writes = [target_stream.write for target_stream in target_streams]
    target_flushes = [target_stream.flush for target_stream in target_streams]

    def patched_write(self, message):
        for target_write in target_writes:
            target_write(message)

    def patched_flush(self):
        for target_flush in target_flushes:
            target_flush()

    original_source_stream_writes = [
        source_stream.write for source_stream in source_streams]
    original_source_stream_flushes = [
        source_stream.flush for source_stream in source_streams]
    try:
        for source_stream in source_streams:
            source_stream.write = MethodType(patched_write, source_stream)
            source_stream.flush = MethodType(patched_flush, source_stream)
        yield
    finally:
        for source_stream, original_source_stream_write, original_source_stream_flush in zip(
            source_streams, original_source_stream_writes, original_source_stream_flushes
        ):
            source_stream.write = original_source_stream_write
            source_stream.flush = original_source_stream_flush


@contextmanager
def log_std_streams(filepath):
    log_file = open(filepath, 'w', encoding='utf-8')
    try:
        with redirect_streams(source_streams=[sys.stdout], target_streams=[log_file, sys.stdout]):
            with redirect_streams(source_streams=[sys.stderr], target_streams=[log_file, sys.stderr]):
                yield
    finally:
        log_file.close()


def arg_name_python_to_cli(arg_name, cli_sep='-'):
    arg_name = arg_name.replace('_', cli_sep)
    return f'--{arg_name}'


def kwargs_to_cli_args_list(kwargs, cli_sep='-'):
    cli_args_list = []
    for key, val in kwargs.items():
        key = arg_name_python_to_cli(key, cli_sep)
        if isinstance(val, bool):
            cli_args_list.append(str(key))
        else:
            if isinstance(val, str):
                # Add quotes around val
                assert "'" not in val
                val = f"'{val}'"
            cli_args_list.extend([str(key), str(val)])
    return cli_args_list


def args_dict_to_str(args_dict, cli_sep='-'):
    return ' '.join(kwargs_to_cli_args_list(args_dict, cli_sep=cli_sep))

# %%


@contextmanager
def log_action(action_description):
    start_time = time.time()
    print(f'{action_description}...')
    try:
        yield
    except BaseException as e:
        print(f'{action_description} failed after {time.time() - start_time:.2f}s.')
        raise e
    print(f'{action_description} completed after {time.time() - start_time:.2f}s.')


def print_running_time(func):
    '''Decorator to print running time of function for logging purposes'''

    @wraps(func)  # To preserve the name and path for pickling purposes
    def wrapped_func(*args, **kwargs):
        function_name = getattr(func, '__name__', repr(func))
        with log_action(function_name):
            return func(*args, **kwargs)

    return wrapped_func
# %%


@contextmanager
def mock_cli_args(args):
    current_args = sys.argv
    sys.argv = sys.argv[:1] + args
    yield
    sys.argv = current_args

# %%


dataset = 'uts_nl_query-9fcb6f786a1339d290dde06e16935402_db-9fcb6f786a1339d290dde06e16935402_topk-8_nprobe-16_density-0.6_distance-0.05_filter_ne-False_levenshtein-0.2_simplicity-0.0'

kwargs = get_mbart_kwargs(dataset=dataset, language='nl', use_access=True)
kwargs['train_kwargs']['ngpus'] = 1  # Set this from 8 to 1 for local training
kwargs['train_kwargs']['max_tokens'] = 512  # Lower this number to prevent OOM

#kwargs['train_kwargs']['optimizer'] = 'cpu_adam'
#kwargs['train_kwargs']['cpu-offload'] = True
#kwargs['train_kwargs']['ddp-backend'] = 'fully_sharded'
#kwargs['train_kwargs']['memory-efficient-fp16'] = True
kwargs['train_kwargs']['warmup_updates'] = 1
kwargs['train_kwargs']['total-num-update'] = 2
kwargs['train_kwargs']['max-update'] = 2


@clear_cuda_cache
def fairseq_train(
    preprocessed_dir,
    exp_dir,
    ngpus=1,
    # Batch size across all gpus (taking update freq into account)
    batch_size=8192,
    max_sentences=64,  # Max sentences per GPU
    arch='transformer',
    save_interval_updates=100,
    max_update=50000,
    lr=0.001,
    warmup_updates=4000,
    dropout=0.1,
    lr_scheduler='inverse_sqrt',
    criterion='label_smoothed_cross_entropy',
    seed=None,
    fp16=True,
    **kwargs,
):
    with log_std_streams(exp_dir / 'fairseq_train.stdout'):
        exp_dir = Path(exp_dir)
        preprocessed_dir = Path(preprocessed_dir)
        exp_dir.mkdir(exist_ok=True, parents=True)
        # Copy dictionaries to exp_dir for generation
        for dict_path in preprocessed_dir.glob('dict.*.txt'):
            shutil.copy(dict_path, exp_dir)
        checkpoints_dir = exp_dir / 'checkpoints'
        total_real_batch_size = max_sentences * ngpus
        update_freq = int(round(batch_size / total_real_batch_size, 0))
        if seed is None:
            seed = random.randint(0, 1000)
        distributed_port = random.randint(10000, 20000)
        #lr_scheduler = 'inverse_sqrt'
        args = f'''
        {preprocessed_dir} --task translation --source-lang complex --target-lang simple --save-dir {checkpoints_dir}
        --optimizer adam --adam-betas '(0.9, 0.98)' --clip-norm 0.0
        --criterion {criterion} --label-smoothing 0.1
        --lr-scheduler {lr_scheduler} --lr {lr} --warmup-updates {warmup_updates} --update-freq {update_freq}
        --arch {arch} --dropout {dropout} --weight-decay 0.0 --clip-norm 0.1 --share-all-embeddings
        --no-epoch-checkpoints --save-interval 999999 --validate-interval 999999
        --max-update {max_update} --save-interval-updates {save_interval_updates} --keep-interval-updates 1 --patience 10
        --batch-size {max_sentences} --seed {seed}
        --distributed-world-size {ngpus} --distributed-port {distributed_port}
        '''
        if lr_scheduler == 'inverse_sqrt':
            args += '--warmup-init-lr 1e-07'
        if fp16:
            args += f' --fp16'
        # FIXME: if the kwargs are already present in the args string, they will appear twice but fairseq will take only the last one into account
        args += f' {args_dict_to_str(kwargs)}'
        args = remove_multiple_whitespaces(args.replace('\n', ' ')).strip(' ')
        # Recover lost quotes around adam betas
        args = re.sub(r'--adam-betas (\(0\.\d+, 0\.\d+\))',
                      r"--adam-betas '\1'", args)
        print(f'fairseq-train {args}')
        with mock_cli_args(shlex.split(args)):
            train.cli_main()


# %%

preprocessed_dir = './resources/datasets/fairseq_preprocessed_complex-simple'
exp_dir = prepare_exp_dir()
train_kwargs = kwargs.get('train_kwargs', {})

print_running_time(fairseq_train)(
    preprocessed_dir, exp_dir=exp_dir, **train_kwargs)