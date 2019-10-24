from dataloader import EvalDataset, TrainDataset, NewBidirectionalOneShotIterator
from dataloader import get_dataset

import dgl
import dgl.backend as F
import argparse
import os
import logging
import time

backend = os.environ.get('DGLBACKEND')
if backend.lower() == 'mxnet':
    import multiprocessing as mp
    from train_mxnet import load_model
    from train_mxnet import train
    from train_mxnet import test
else:
    import torch.multiprocessing as mp
    from train_pytorch import load_model
    from train_pytorch import multi_gpu_train
    from train_pytorch import test, multi_gpu_test

class ArgParser(argparse.ArgumentParser):
    def __init__(self):
        super(ArgParser, self).__init__()

        self.add_argument('--model_name', default='TransE',
                          choices=['TransE', 'TransH', 'TransR', 'TransD',
                                   'RESCAL', 'DistMult', 'ComplEx', 'RotatE', 'pRotatE'],
                          help='model to use')
        self.add_argument('--data_path', type=str, default='data',
                          help='root path of all dataset')
        self.add_argument('--dataset', type=str, default='FB15k',
                          help='dataset name, under data_path')
        self.add_argument('--format', type=str, default='1',
                          help='the format of the dataset.')
        self.add_argument('--save_path', type=str, default='ckpts',
                          help='place to save models and logs')
        self.add_argument('--save_emb', type=str, default=None,
                          help='save the embeddings in the specific location.')

        self.add_argument('--max_step', type=int, default=80000,
                          help='train xx steps')
        self.add_argument('--warm_up_step', type=int, default=None,
                          help='for learning rate decay')
        self.add_argument('--batch_size', type=int, default=1024,
                          help='batch size')
        self.add_argument('--batch_size_eval', type=int, default=8,
                          help='batch size used for eval and test')
        self.add_argument('--neg_sample_size', type=int, default=128,
                          help='negative sampling size')
        self.add_argument('--neg_sample_size_valid', type=int, default=1000,
                          help='negative sampling size for validation')
        self.add_argument('--neg_sample_size_test', type=int, default=-1,
                          help='negative sampling size for testing')
        self.add_argument('--hidden_dim', type=int, default=256,
                          help='hidden dim used by relation and entity')
        self.add_argument('--lr', type=float, default=0.0001,
                          help='learning rate')
        self.add_argument('-g', '--gamma', type=float, default=12.0,
                          help='margin value')
        self.add_argument('--eval_percent', type=float, default=1,
                          help='sample some percentage for evaluation.')

        self.add_argument('--gpu', type=int, default=-1,
                          help='use GPU')
        self.add_argument('--mix_cpu_gpu', action='store_true',
                          help='mix CPU and GPU training')
        self.add_argument('-de', '--double_ent', action='store_true',
                          help='double entitiy dim for complex number')
        self.add_argument('-dr', '--double_rel', action='store_true',
                          help='double relation dim for complex number')
        self.add_argument('--seed', type=int, default=0,
                          help='set random seed fro reproducibility')
        self.add_argument('-log', '--log_interval', type=int, default=1000,
                          help='do evaluation after every x steps')
        self.add_argument('--eval_interval', type=int, default=10000,
                          help='do evaluation after every x steps')
        self.add_argument('-adv', '--neg_adversarial_sampling', action='store_true',
                          help='if use negative adversarial sampling')
        self.add_argument('-a', '--adversarial_temperature', default=1.0, type=float)

        self.add_argument('--valid', action='store_true',
                          help='if valid a model')
        self.add_argument('--test', action='store_true',
                          help='if test a model')
        self.add_argument('-rc', '--regularization_coef', type=float, default=0.000002,
                          help='set value > 0.0 if regularization is used')
        self.add_argument('-rn', '--regularization_norm', type=int, default=3,
                          help='norm used in regularization')
        self.add_argument('--num_worker', type=int, default=16,
                          help='number of workers used for loading data')
        self.add_argument('--non_uni_weight', action='store_true',
                          help='if use uniform weight when computing loss')
        self.add_argument('--init_step', type=int, default=0,
                          help='DONT SET MANUALLY, used for resume')
        self.add_argument('--step', type=int, default=0,
                          help='DONT SET MANUALLY, track current step')
        self.add_argument('--pickle_graph', action='store_true',
                          help='pickle built graph, building a huge graph is slow.')
        self.add_argument('--num_proc', type=int, default=1,
                          help='number of process used')
        self.add_argument('--rel_part', action='store_true',
                          help='enable relation partitioning')


def get_logger(args):
    if not os.path.exists(args.save_path):
        os.mkdir(args.save_path)

    folder = '{}_{}_'.format(args.model_name, args.dataset)
    n = len([x for x in os.listdir(args.save_path) if x.startswith(folder)])
    folder += str(n)
    args.save_path = os.path.join(args.save_path, folder)

    if not os.path.exists(args.save_path):
        os.makedirs(args.save_path)
    log_file = os.path.join(args.save_path, 'train.log')

    logging.basicConfig(
        format='%(asctime)s %(levelname)-8s %(message)s',
        level=logging.INFO,
        datefmt='%Y-%m-%d %H:%M:%S',
        filename=log_file,
        filemode='w'
    )

    logger = logging.getLogger(__name__)
    print("Logs are being recorded at: {}".format(log_file))
    return logger

def run_server(num_worker, graph, etype_id):
    g = dgl.contrib.graph_store.create_graph_store_server(graph, "Test", "shared_mem",
                    num_worker, False, edge_dir='in')
    g.ndata['id'] = F.arange(0, graph.number_of_nodes())
    g.edata['id'] = F.tensor(etype_id, F.int64)
    g.run()

def run(args, logger):
    # load dataset and samplers
    dataset = get_dataset(args.data_path, args.dataset, args.format)
    n_entities = dataset.n_entities
    n_relations = dataset.n_relations
    if args.neg_sample_size_test < 0:
        args.neg_sample_size_test = n_entities

    train_data = TrainDataset(dataset, args, ranks=args.num_proc)
    if args.valid or args.test:
        eval_dataset = EvalDataset(dataset, args)

    if args.valid or args.test:
        num_connection = args.num_proc * 2 if args.valid and args.test else args.num_proc
        proc = mp.Process(target=run_server, args=(num_connection, eval_dataset.g, eval_dataset.etype_id))
        proc.start()

    # We need to free all memory referenced by dataset.
    if args.test:
        test_edges = eval_dataset.get_edges('test')
    if args.valid:
        valid_edges = eval_dataset.get_edges('valid')
    else:
        valid_edges = None

    eval_dataset = None
    dataset = None

    # load model
    model = load_model(logger, args, n_entities, n_relations)

    if args.num_proc > 1:
        model.share_memory()

    # train
    start = time.time()
    
    if args.num_proc > 1:
        procs = []
        for i in range(args.num_proc):
            g = train_data.graphs[i]
            proc = mp.Process(target=multi_gpu_train, args=(args, model, g, n_entities, valid_edges, i))
            procs.append(proc)
            proc.start()
        for proc in procs:
            proc.join()
    else:
        g = train_data.graphs[0]
        multi_gpu_train(args, model, g, n_entities, valid_edges, 0)
    print('training takes {} seconds'.format(time.time() - start))
    
    if args.save_emb is not None:
        if not os.path.exists(args.save_emb):
            os.mkdir(args.save_emb)
        model.save_emb(args.save_emb, args.dataset)

    # test
    if args.test:
        if args.num_proc > 1:
            procs = []
            for i in range(args.num_proc):
                proc = mp.Process(target=multi_gpu_test, args=(args, model, 'Test', test_edges, i))
                procs.append(proc)
                proc.start()
            for proc in procs:
                proc.join()
        else:
            multi_gpu_test(args, model, 'Test', test_edges, 0)

if __name__ == '__main__':
    args = ArgParser().parse_args()
    logger = get_logger(args)
    run(args, logger)
