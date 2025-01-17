"""
call modifications from fast5 files or extracted features,
using tensorflow and the trained model.
output format: chromosome, pos, strand, pos_in_strand, read_name, read_strand,
prob_0, prob_1, called_label, seq
"""

from __future__ import absolute_import

import torch
import argparse
import os
import sys
import numpy as np
from sklearn import metrics

import gzip

# import multiprocessing as mp
import torch.multiprocessing as mp

import time

from .models import ModelBiLSTM
from .utils.process_utils import base2code_dna
from .utils.process_utils import code2base_dna
from .utils.process_utils import str2bool
from .utils.process_utils import display_args
from .utils.process_utils import nproc_to_call_mods_in_cpu_mode

from .extract_features import _extract_features
from .extract_features import _extract_preprocess_
from .extract_features import _extract_preprocess_fast5sinfo

from .utils.constants_torch import FloatTensor
from .utils.constants_torch import use_cuda

import uuid

if use_cuda:
    # from .utils.process_utils import MyQueue as Queue
    from torch.multiprocessing import Queue
else:
    from .utils.process_utils import MyQueue as Queue

# add this export temporarily
# https://github.com/pytorch/pytorch/issues/37377
os.environ['MKL_THREADING_LAYER'] = 'GNU'

queue_size_border_f5batch = 100
queue_size_border = 1000
time_wait = 1


def _read_features_file(features_file, features_batch_q, f5_batch_size=10):
    """
    read features of samples from file generated by extract_features.py to a group of lists
    :param features_file: file generated by extract_features.py
    :param features_batch_q: Queue for saving features
    :param f5_batch_size: f5 batch size
    :return: save features in Queue/features_batch_q
    """
    print("read_features process-{} starts".format(os.getpid()))
    r_num, b_num = 0, 0

    if features_file.endswith(".gz"):
        infile = gzip.open(features_file, 'rt')
    else:
        infile = open(features_file, 'r')

    sampleinfo = []  # contains: chromosome, pos, strand, pos_in_strand, read_name, read_strand
    kmers = []
    base_means = []
    base_stds = []
    base_signal_lens = []
    k_signals = []
    labels = []

    line = next(infile)
    words = line.strip().split("\t")
    readid_pre = words[4]

    sampleinfo.append("\t".join(words[0:6]))
    kmers.append([base2code_dna[x] for x in words[6]])
    base_means.append([float(x) for x in words[7].split(",")])
    base_stds.append([float(x) for x in words[8].split(",")])
    base_signal_lens.append([int(x) for x in words[9].split(",")])
    k_signals.append([[float(y) for y in x.split(",")] for x in words[10].split(";")])
    labels.append(int(words[11]))

    for line in infile:
        words = line.strip().split("\t")
        readidtmp = words[4]
        if readidtmp != readid_pre:
            r_num += 1
            readid_pre = readidtmp
            if r_num % f5_batch_size == 0:
                features_batch_q.put((sampleinfo, kmers, base_means, base_stds,
                                      base_signal_lens, k_signals, labels))
                while features_batch_q.qsize() > queue_size_border_f5batch:
                    time.sleep(time_wait)
                sampleinfo = []
                kmers = []
                base_means = []
                base_stds = []
                base_signal_lens = []
                k_signals = []
                labels = []
                b_num += 1

        sampleinfo.append("\t".join(words[0:6]))
        kmers.append([base2code_dna[x] for x in words[6]])
        base_means.append([float(x) for x in words[7].split(",")])
        base_stds.append([float(x) for x in words[8].split(",")])
        base_signal_lens.append([int(x) for x in words[9].split(",")])
        k_signals.append([[float(y) for y in x.split(",")] for x in words[10].split(";")])
        labels.append(int(words[11]))
    infile.close()
    r_num += 1
    if len(sampleinfo) > 0:
        features_batch_q.put((sampleinfo, kmers, base_means, base_stds,
                              base_signal_lens, k_signals, labels))
        b_num += 1
    features_batch_q.put("kill")
    print("read_features process-{} ending, read {} reads in {} f5-batches({})".format(os.getpid(),
                                                                                       r_num, b_num,
                                                                                       f5_batch_size))


def _call_mods(features_batch, model, batch_size, device=0):
    """
    call modification from a batch of features
    :param features_batch:  a bathc of features, contains lists
    :param model: torch model
    :param batch_size: batch size
    :return: pred_str
    """
    # features_batch: 1. if from _read_features_file(), has 1 * args.batch_size samples (not any more, modified)
    # --------------: 2. if from _read_features_from_fast5s(), has uncertain number of samples
    sampleinfo, kmers, base_means, base_stds, base_signal_lens, \
        k_signals, labels = features_batch
    labels = np.reshape(labels, (len(labels)))

    pred_str = []
    accuracys = []
    batch_num = 0
    for i in np.arange(0, len(sampleinfo), batch_size):
        batch_s, batch_e = i, i + batch_size
        b_sampleinfo = sampleinfo[batch_s:batch_e]
        b_kmers = kmers[batch_s:batch_e]
        b_base_means = base_means[batch_s:batch_e]
        b_base_stds = base_stds[batch_s:batch_e]
        b_base_signal_lens = base_signal_lens[batch_s:batch_e]
        b_k_signals = k_signals[batch_s:batch_e]
        b_labels = labels[batch_s:batch_e]

        # call mods of each batch
        if len(b_sampleinfo) > 0:
            voutputs, vlogits = model(FloatTensor(b_kmers, device), FloatTensor(b_base_means, device),
                                      FloatTensor(b_base_stds, device),
                                      FloatTensor(b_base_signal_lens, device),
                                      FloatTensor(b_k_signals, device))
            _, vpredicted = torch.max(vlogits.data, 1)
            if use_cuda:
                vlogits = vlogits.cpu()
                vpredicted = vpredicted.cpu()

            predicted = vpredicted.numpy()
            logits = vlogits.data.numpy()

            acc_batch = metrics.accuracy_score(
                y_true=b_labels, y_pred=predicted)
            accuracys.append(acc_batch)

            for idx in range(len(b_sampleinfo)):
                # chromosome, pos, strand, pos_in_strand, read_name, read_strand, prob_0, prob_1, called_label, seq
                prob_0, prob_1 = logits[idx][0], logits[idx][1]
                prob_0_norm = round(prob_0 / (prob_0 + prob_1), 6)
                prob_1_norm = round(1 - prob_0_norm, 6)
                # kmer-5
                b_idx_kmer = ''.join([code2base_dna[x] for x in b_kmers[idx]])
                center_idx = int(np.floor(len(b_idx_kmer) / 2))
                bkmer_start = center_idx - 2 if center_idx - 2 >= 0 else 0
                bkmer_end = center_idx + 3 if center_idx + 3 <= len(b_idx_kmer) else len(b_idx_kmer)

                pred_str.append("\t".join([b_sampleinfo[idx], str(prob_0_norm),
                                           str(prob_1_norm), str(predicted[idx]),
                                           b_idx_kmer[bkmer_start:bkmer_end]]))
            batch_num += 1
    accuracy = np.mean(accuracys) if len(accuracys) > 0 else 0

    return pred_str, accuracy, batch_num


def _call_mods_q(model_path, features_batch_q, pred_str_q, success_file, args, device=0):
    """
    subprocess for calling modifications
    :param model_path:
    :param features_batch_q: Queue
    :param pred_str_q: Queue
    :param success_file: no_use for now
    :param args: argparse object
    :return: call_mods from features_batch_q to pred_str_q
    """
    print('call_mods process-{} starts'.format(os.getpid()))

    # TODO: when using multi-gpu, the process seems to not work as expected.
    #  models not in the 1st gpu card tend to occupy memory of
    #  both the 1st gpu card and the card where the model is in,
    #  especially at the time when the process is about to ending.
    #  [In torch 1.6.0, 1.7.0, 1.8.0, 1.11.0, the models seem to only occupy
    #  memory of both cards at the end, while in 1.9.0/1.10.0, the models
    #  occupy memory of both cards all the time.]
    model = ModelBiLSTM(args.seq_len, args.signal_len, args.layernum1, args.layernum2, args.class_num,
                        args.dropout_rate, args.hid_rnn,
                        args.n_vocab, args.n_embed, str2bool(args.is_base), str2bool(args.is_signallen),
                        module=args.model_type, device=device)

    para_dict = torch.load(model_path, map_location=torch.device('cpu'))
    # para_dict = torch.load(model_path, map_location=torch.device(device))
    model_dict = model.state_dict()
    model_dict.update(para_dict)
    model.load_state_dict(model_dict)
    del model_dict

    if use_cuda:
        model = model.cuda(device)
    model.eval()

    accuracy_list = []
    batch_num_total = 0
    while True:
        # if os.path.exists(success_file):
        #     break

        if features_batch_q.empty():
            time.sleep(time_wait)
            continue

        features_batch = features_batch_q.get()
        if features_batch == "kill":
            # deprecate successfile, use "kill" signal multi times to kill each process
            features_batch_q.put("kill")
            # open(success_file, 'w').close()
            break

        pred_str, accuracy, batch_num = _call_mods(features_batch, model, args.batch_size, device)

        pred_str_q.put(pred_str)
        while pred_str_q.qsize() > queue_size_border:
            time.sleep(time_wait)
        # for debug
        # print("call_mods process-{} reads 1 batch, features_batch_q:{}, "
        #       "pred_str_q: {}".format(os.getpid(), features_batch_q.qsize(), pred_str_q.qsize()))
        accuracy_list.append(accuracy)
        batch_num_total += batch_num
    # print('total accuracy in process {}: {}'.format(os.getpid(), np.mean(accuracy_list)))
    print('call_mods process-{} ending, proceed {} feature-batches({})'.format(os.getpid(), batch_num_total,
                                                                               args.batch_size))


def _write_predstr_to_file(write_fp, predstr_q, is_gzip):
    print('write_process-{} starts'.format(os.getpid()))
    if is_gzip:
        if not write_fp.endswith(".gz"):
            write_fp += ".gz"
        wf = gzip.open(write_fp, "wt")
    else:
        wf = open(write_fp, 'w')
    while True:
        # during test, it's ok without the sleep()
        if predstr_q.empty():
            time.sleep(time_wait)
            continue
        pred_str = predstr_q.get()
        if pred_str == "kill":
            wf.close()
            print('write_process-{} finished'.format(os.getpid()))
            break
        for one_pred_str in pred_str:
            wf.write(one_pred_str + "\n")
        wf.flush()


def _read_features_from_fast5s(fast5s, motif_seqs, chrom2len, positions, regioninfo, args):
    """
    get features from a group of fast5s
    :param fast5s: a list of fast5 files
    :param motif_seqs:
    :param chrom2len:
    :param positions:
    :param regioninfo:
    :param args:
    :return: a list of feature bataches
    """
    features_list, error = _extract_features(fast5s, args.corrected_group, args.basecall_subgroup,
                                             args.normalize_method, motif_seqs, args.mod_loc, chrom2len,
                                             args.seq_len, args.signal_len,
                                             1, positions, regioninfo)
    features_batches = []

    sampleinfo = []  # contains: chromosome, pos, strand, pos_in_strand, read_name, read_strand
    kmers = []
    base_means = []
    base_stds = []
    base_signal_lens = []
    k_signals = []
    labels = []
    for features in features_list:
        chrom, pos, alignstrand, loc_in_ref, readname, strand, k_mer, signal_means, signal_stds, \
                    signal_lens, kmer_base_signals, f_methy_label = features

        sampleinfo.append("\t".join([chrom, str(pos), alignstrand, str(loc_in_ref), readname, strand]))
        kmers.append([base2code_dna[x] for x in k_mer])
        base_means.append(signal_means)
        base_stds.append(signal_stds)
        base_signal_lens.append(signal_lens)
        k_signals.append(kmer_base_signals)
        labels.append(f_methy_label)
    features_batches.append((sampleinfo, kmers, base_means, base_stds,
                             base_signal_lens, k_signals, labels))
    return features_batches, error


def _read_features_fast5s_q(fast5s_q, features_batch_q, errornum_q,
                            motif_seqs, chrom2len, positions, regioninfo,
                            args):
    """
    subprocess for reading features from fast5 files
    :param fast5s_q:
    :param features_batch_q:
    :param errornum_q:
    :param motif_seqs:
    :param chrom2len:
    :param positions:
    :param regioninfo:
    :param args:
    :return: from fast5s_q to features_batch_q
    """
    print("read_fast5 process-{} starts".format(os.getpid()))
    f5_num = 0
    while True:
        if fast5s_q.empty():
            time.sleep(time_wait)
        fast5s = fast5s_q.get()
        if fast5s == "kill":
            fast5s_q.put("kill")
            break
        f5_num += len(fast5s)
        features_batches, error = _read_features_from_fast5s(fast5s, motif_seqs, chrom2len, positions, regioninfo,
                                                             args)
        errornum_q.put(error)
        for features_batch in features_batches:
            features_batch_q.put(features_batch)
        while features_batch_q.qsize() > queue_size_border_f5batch:
            time.sleep(time_wait)
    print("read_fast5 process-{} ending, proceed {} fast5s".format(os.getpid(), f5_num))


def _call_mods_from_fast5s_gpu(motif_seqs, chrom2len, fast5s_q, len_fast5s, positions, regioninfo,
                               model_path, success_file,
                               args):
    """
    call modification from fast5 files using gpu
    :param motif_seqs:
    :param chrom2len:
    :param fast5s_q: fast5 files queue
    :param len_fast5s:
    :param positions:
    :param regioninfo:
    :param model_path:
    :param success_file:
    :param args: from fast5s_q to file
    :return:
    """
    # features_batch_q = mp.Queue()
    # errornum_q = mp.Queue()
    features_batch_q = Queue()
    errornum_q = Queue()

    # pred_str_q = mp.Queue()
    pred_str_q = Queue()

    nproc = args.nproc
    nproc_gpu = args.nproc_gpu
    if nproc_gpu < 1:
        nproc_gpu = 1
    if nproc <= nproc_gpu + 1:
        print("--nproc must be >= --nproc_gpu + 2!!")
        nproc = nproc_gpu + 1 + 1

    fast5s_q.put("kill")
    # queues of fast5s->features
    features_batch_procs = []
    for _ in range(nproc - nproc_gpu - 1):
        p = mp.Process(target=_read_features_fast5s_q, args=(fast5s_q, features_batch_q, errornum_q,
                                                             motif_seqs, chrom2len, positions, regioninfo,
                                                             args))
        p.daemon = True
        p.start()
        features_batch_procs.append(p)

    # queues of features->mods_call
    call_mods_gpu_procs = []
    gpulist = _get_gpus()
    gpuindex = 0
    for _ in range(nproc_gpu):
        p_call_mods_gpu = mp.Process(target=_call_mods_q, args=(model_path, features_batch_q, pred_str_q,
                                                                success_file, args, gpulist[gpuindex]))
        gpuindex += 1
        p_call_mods_gpu.daemon = True
        p_call_mods_gpu.start()
        call_mods_gpu_procs.append(p_call_mods_gpu)

    # queue of writing
    # print("write_process started..")
    p_w = mp.Process(target=_write_predstr_to_file, args=(args.result_file, pred_str_q, args.gzip))
    p_w.daemon = True
    p_w.start()

    errornum_sum = 0
    while True:
        running = any(p.is_alive() for p in features_batch_procs)
        while not errornum_q.empty():
            errornum_sum += errornum_q.get()
        if not running:
            break

    for p in features_batch_procs:
        p.join()
    features_batch_q.put("kill")

    for p_call_mods_gpu in call_mods_gpu_procs:
        p_call_mods_gpu.join()

    # print("finishing the write_process..")
    pred_str_q.put("kill")

    p_w.join()

    print("%d of %d fast5 files failed.." % (errornum_sum, len_fast5s))


def _call_mods_from_fast5s_cpu2(motif_seqs, chrom2len, fast5s_q, len_fast5s, positions, regioninfo,
                                model_path,
                                success_file, args):
    """
    call modification from fast5 files using cpu
    :param motif_seqs:
    :param chrom2len:
    :param fast5s_q:
    :param len_fast5s:
    :param positions:
    :param regioninfo:
    :param model_path:
    :param success_file:
    :param args:
    :return: from fast5s_q to file
    """
    # features_batch_q = mp.Queue()
    # errornum_q = mp.Queue()
    features_batch_q = Queue()
    errornum_q = Queue()

    # pred_str_q = mp.Queue()
    pred_str_q = Queue()

    nproc = args.nproc
    nproc_call_mods = nproc_to_call_mods_in_cpu_mode
    if nproc <= nproc_call_mods + 1:
        nproc = nproc_call_mods + 1 + 1

    fast5s_q.put("kill")
    # queues of features->mods_call
    features_batch_procs = []
    for _ in range(nproc - nproc_call_mods - 1):
        p = mp.Process(target=_read_features_fast5s_q, args=(fast5s_q, features_batch_q, errornum_q,
                                                             motif_seqs, chrom2len, positions, regioninfo,
                                                             args))
        p.daemon = True
        p.start()
        features_batch_procs.append(p)

    # queues of features->mods_call
    call_mods_cpu_procs = []
    for _ in range(nproc_call_mods):
        p_call_mods_cpu = mp.Process(target=_call_mods_q, args=(model_path, features_batch_q, pred_str_q,
                                                                success_file, args))
        p_call_mods_cpu.daemon = True
        p_call_mods_cpu.start()
        call_mods_cpu_procs.append(p_call_mods_cpu)

    # queue of writing
    # print("write_process started..")
    p_w = mp.Process(target=_write_predstr_to_file, args=(args.result_file, pred_str_q, args.gzip))
    p_w.daemon = True
    p_w.start()

    errornum_sum = 0
    while True:
        running = any(p.is_alive() for p in features_batch_procs)
        while not errornum_q.empty():
            errornum_sum += errornum_q.get()
        if not running:
            break

    for p in features_batch_procs:
        p.join()
    features_batch_q.put("kill")

    for p_call_mods_cpu in call_mods_cpu_procs:
        p_call_mods_cpu.join()

    # print("finishing the write_process..")
    pred_str_q.put("kill")

    p_w.join()

    print("%d of %d fast5 files failed.." % (errornum_sum, len_fast5s))


def _get_gpus():
    num_gpus = torch.cuda.device_count()
    if num_gpus > 0:
        gpulist = list(range(num_gpus))
    else:
        gpulist = [0]
    return gpulist * 1000


def call_mods(args):
    """
    main function of calling modification
    :param args: argparse object
    :return: main function
    """

    print("[main] call_mods starts..")
    start = time.time()

    print("cuda availability: {}".format(use_cuda))
    if use_cuda:
        try:
            mp.set_start_method('spawn')
        except RuntimeError:
            raise RuntimeError("torch.multiprocessing -> RuntimeError")

    model_path = os.path.abspath(args.model_path)
    if not os.path.exists(model_path):
        raise ValueError("--model_path is not set right!")
    input_path = os.path.abspath(args.input_path)
    if not os.path.exists(input_path):
        raise ValueError("--input_path does not exist!")
    success_file = input_path.rstrip("/") + "." + str(uuid.uuid1()) + ".success"
    if os.path.exists(success_file):
        os.remove(success_file)

    if os.path.isdir(input_path):  # call modifications from directory that contains fast5s
        # motif_seqs, chrom2len, fast5s_q, len_fast5s, positions, \
        #     regioninfo = _extract_preprocess(input_path,
        #                                      str2bool(args.recursively),
        #                                      args.motifs,
        #                                      str2bool(args.is_dna),
        #                                      args.reference_path,
        #                                      args.f5_batch_size,
        #                                      args.positions,
        #                                      args.region)
        fast5s_q = Queue()
        fast5s_q, len_fast5s = _extract_preprocess_fast5sinfo(input_path, str2bool(args.recursively),
                                                              args.f5_batch_size, fast5s_q)

        motif_seqs, chrom2len, positions, regioninfo = _extract_preprocess_(args.motifs, str2bool(args.is_dna),
                                                                            args.reference_path,
                                                                            args.positions, args.region)
        if use_cuda:
            _call_mods_from_fast5s_gpu(motif_seqs, chrom2len, fast5s_q, len_fast5s, positions, regioninfo,
                                       model_path,
                                       success_file, args)
        else:
            _call_mods_from_fast5s_cpu2(motif_seqs, chrom2len, fast5s_q, len_fast5s, positions, regioninfo,
                                        model_path,
                                        success_file, args)
    else:  # call modifications from feature_file
        # features_batch_q = mp.Queue()
        features_batch_q = Queue()
        p_rf = mp.Process(target=_read_features_file, args=(input_path, features_batch_q,
                                                            args.f5_batch_size))
        p_rf.daemon = True
        p_rf.start()

        # pred_str_q = mp.Queue()
        pred_str_q = Queue()

        predstr_procs = []

        nproc = args.nproc
        if nproc < 3:
            print("--nproc must be >= 3!!")
            nproc = 3
        if use_cuda:
            nproc_dp = args.nproc_gpu
            if nproc_dp > nproc - 2:
                print("--nproc_gpu must be <= --nproc - 2!!")
                nproc_dp = nproc - 2
            if nproc_dp < 1:
                nproc_dp = 1
        else:
            nproc_dp = nproc - 2
            if nproc_dp > nproc_to_call_mods_in_cpu_mode:
                nproc_dp = nproc_to_call_mods_in_cpu_mode

        gpulist = _get_gpus()
        gpuindex = 0
        for _ in range(nproc_dp):
            p = mp.Process(target=_call_mods_q, args=(model_path, features_batch_q, pred_str_q,
                                                      success_file, args, gpulist[gpuindex]))
            gpuindex += 1
            p.daemon = True
            p.start()
            predstr_procs.append(p)

        # print("write_process started..")
        p_w = mp.Process(target=_write_predstr_to_file, args=(args.result_file, pred_str_q, args.gzip))
        p_w.daemon = True
        p_w.start()

        for p in predstr_procs:
            p.join()

        # print("finishing the write_process..")
        pred_str_q.put("kill")

        p_rf.join()

        p_w.join()

    if os.path.exists(success_file):
        os.remove(success_file)
    print("[main] call_mods costs %.2f seconds.." % (time.time() - start))


def main():
    parser = argparse.ArgumentParser("call modifications")

    p_input = parser.add_argument_group("INPUT")
    p_input.add_argument("--input_path", "-i", action="store", type=str,
                         required=True,
                         help="the input path, can be a signal_feature file from extract_features.py, "
                              "or a directory of fast5 files. If a directory of fast5 files is provided, "
                              "args in FAST5_EXTRACTION should be provided.")
    p_input.add_argument("--f5_batch_size", action="store", type=int, default=30,
                         required=False,
                         help="number of reads/files to be processed by each process one time, default 30")

    p_call = parser.add_argument_group("CALL")
    p_call.add_argument("--model_path", "-m", action="store", type=str, required=True,
                        help="file path of the trained model (.ckpt)")

    # model input
    p_call.add_argument('--model_type', type=str, default="both_bilstm",
                        choices=["both_bilstm", "seq_bilstm", "signal_bilstm"],
                        required=False,
                        help="type of model to use, 'both_bilstm', 'seq_bilstm' or 'signal_bilstm', "
                             "'both_bilstm' means to use both seq and signal bilstm, default: both_bilstm")
    p_call.add_argument('--seq_len', type=int, default=13, required=False,
                        help="len of kmer. default 13")
    p_call.add_argument('--signal_len', type=int, default=16, required=False,
                        help="signal num of one base, default 16")

    # model param
    p_call.add_argument('--layernum1', type=int, default=3,
                        required=False, help="lstm layer num for combined feature, default 3")
    p_call.add_argument('--layernum2', type=int, default=1,
                        required=False, help="lstm layer num for seq feature (and for signal feature too), default 1")
    p_call.add_argument('--class_num', type=int, default=2, required=False)
    p_call.add_argument('--dropout_rate', type=float, default=0, required=False)
    p_call.add_argument('--n_vocab', type=int, default=16, required=False,
                        help="base_seq vocab_size (15 base kinds from iupac)")
    p_call.add_argument('--n_embed', type=int, default=4, required=False,
                        help="base_seq embedding_size")
    p_call.add_argument('--is_base', type=str, default="yes", required=False,
                        help="is using base features in seq model, default yes")
    p_call.add_argument('--is_signallen', type=str, default="yes", required=False,
                        help="is using signal length feature of each base in seq model, default yes")

    p_call.add_argument("--batch_size", "-b", default=512, type=int, required=False,
                        action="store", help="batch size, default 512")

    # BiLSTM model param
    p_call.add_argument('--hid_rnn', type=int, default=256, required=False,
                        help="BiLSTM hidden_size for combined feature")

    p_output = parser.add_argument_group("OUTPUT")
    p_output.add_argument("--result_file", "-o", action="store", type=str, required=True,
                          help="the file path to save the predicted result")
    p_output.add_argument("--gzip", action="store_true", default=False, required=False,
                          help="if compressing the output using gzip")

    p_f5 = parser.add_argument_group("FAST5_EXTRACTION")
    p_f5.add_argument("--recursively", "-r", action="store", type=str, required=False,
                      default='yes', help='is to find fast5 files from fast5 dir recursively. '
                                          'default true, t, yes, 1')
    p_f5.add_argument("--corrected_group", action="store", type=str, required=False,
                      default='RawGenomeCorrected_000',
                      help='the corrected_group of fast5 files after '
                           'tombo re-squiggle. default RawGenomeCorrected_000')
    p_f5.add_argument("--basecall_subgroup", action="store", type=str, required=False,
                      default='BaseCalled_template',
                      help='the corrected subgroup of fast5 files. default BaseCalled_template')
    p_f5.add_argument("--is_dna", action="store", type=str, required=False,
                      default='yes',
                      help='whether the fast5 files from DNA sample or not. '
                           'default true, t, yes, 1. '
                           'setting this option to no/false/0 means '
                           'the fast5 files are from RNA sample.')
    p_f5.add_argument("--normalize_method", action="store", type=str, choices=["mad", "zscore"],
                      default="mad", required=False,
                      help="the way for normalizing signals in read level. "
                           "mad or zscore, default mad")
    # p_f5.add_argument("--methy_label", action="store", type=int,
    #                   choices=[1, 0], required=False, default=1,
    #                   help="the label of the interested modified bases, this is for training."
    #                        " 0 or 1, default 1")
    p_f5.add_argument("--motifs", action="store", type=str,
                      required=False, default='CG',
                      help='motif seq to be extracted, default: CG. '
                           'can be multi motifs splited by comma '
                           '(no space allowed in the input str), '
                           'or use IUPAC alphabet, '
                           'the mod_loc of all motifs must be '
                           'the same')
    p_f5.add_argument("--mod_loc", action="store", type=int, required=False, default=0,
                      help='0-based location of the targeted base in the motif, default 0')
    p_f5.add_argument("--region", action="store", type=str,
                      required=False, default=None,
                      help="region of interest, e.g.: chr1, chr1:0, chr1:0-10000. "
                           "0-based, half-open interval: [start, end). "
                           "default None, means processing the whole sites in genome")
    p_f5.add_argument("--positions", action="store", type=str,
                      required=False, default=None,
                      help="file with a list of positions interested (must be formatted as tab-separated file"
                           " with chromosome, position (in fwd strand), and strand. motifs/mod_loc are still "
                           "need to be set. --positions is used to narrow down the range of the trageted "
                           "motif locs. default None")
    p_f5.add_argument("--reference_path", action="store",
                      type=str, required=False, default=None,
                      help="the reference file to be used, usually is a .fa file. (not necessary)")

    parser.add_argument("--nproc", "-p", action="store", type=int, default=10,
                        required=False, help="number of processes to be used, default 10.")
    parser.add_argument("--nproc_gpu", action="store", type=int, default=2,
                        required=False, help="number of processes to use gpu (if gpu is available), "
                                             "1 or a number less than nproc-1, no more than "
                                             "nproc/4 is suggested. default 2.")
    # parser.add_argument("--is_gpu", action="store", type=str, default="no", required=False,
    #                     choices=["yes", "no"], help="use gpu for tensorflow or not, default no. "
    #                                                 "If you're using a gpu machine, please set to yes. "
    #                                                 "Note that when is_gpu is yes, --nproc is not valid "
    #                                                 "to tensorflow.")

    args = parser.parse_args()
    display_args(args)

    call_mods(args)


if __name__ == '__main__':
    sys.exit(main())
