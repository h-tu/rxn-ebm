import argparse
import logging
import traceback
import os
import sys
import random
import torch
import torch.nn as nn
import torch.distributed as dist
import torch.multiprocessing as mp
import gc
gc.enable() 

from functools import partial
from datetime import datetime, date
from rdkit import RDLogger
from typing import Optional, Dict, List, Union

from rxnebm.data import dataset
from rxnebm.experiment import expt, expt_dist, expt_mixture, expt_utils
from rxnebm.model import FF, G2E, S2E, model_utils

torch.backends.cudnn.benchmark = False # turn off for G2E
torch.backends.cudnn.deterministic = True

try:
    send_message = partial(expt_utils.send_message, 
                       chat_id=os.environ['CHAT_ID'], 
                       bot_token=os.environ['BOT_TOKEN'])
except Exception as e:
    pass

def parse_args():
    parser = argparse.ArgumentParser("trainEBM.py")
    # mode & metadata
    parser.add_argument("--model_name", help="model name", type=str, default="FeedforwardEBM")
    parser.add_argument("--do_not_train", help="do not do any training? (to just test/get energies)", action="store_true")
    parser.add_argument("--do_pretrain", help="whether to pretrain (vs. finetune)", action="store_true")
    parser.add_argument("--do_finetune", help="whether to finetune (vs. pretrain)", action="store_true")
    parser.add_argument("--do_train_mixture", help="whether training mixture model", action="store_true")
    parser.add_argument("--do_test", help="whether to test after training", action="store_true")
    parser.add_argument("--do_get_energies_and_acc", help="whether to test after training", action="store_true")
    parser.add_argument("--do_compute_graph_feat", help="whether to compute graph features", action="store_true")
    parser.add_argument("--onthefly", help="whether to do on-the-fly computation", action="store_true")
    parser.add_argument("--load_checkpoint", help="whether to load from checkpoint", action="store_true")
    parser.add_argument("--test_on_train", help="whether to evaluate on the training data", action="store_true")
    parser.add_argument("--date_trained", help="date trained (DD_MM_YYYY)", type=str, default=date.today().strftime("%d_%m_%Y"))
    parser.add_argument("--load_epoch", help="epoch to load from", type=int)
    parser.add_argument("--testing_best_ckpt", help="helper arg indicating if we are just testing best ckpt. You shouldn't need to touch this", action="store_true")
    parser.add_argument("--expt_name", help="experiment name", type=str)
    parser.add_argument("--old_expt_name", help="old experiment name", type=str)
    parser.add_argument("--checkpoint_folder", help="checkpoint folder", type=str)
    parser.add_argument("--checkpoint_root", help="checkpoint root (it is a subset of checkpoint_folder: checkpoint_folder = checkpoint_root / date_trained)", type=str)
    parser.add_argument("--root", help="input data folder, if None it will be set to default rxnebm/data/cleaned_data/", type=str)
    parser.add_argument("--torch_anomaly", help='whether to set torch.autograd.set_detect_anomaly(True)', action="store_true")
    # distributed arguments
    parser.add_argument("--dataparallel", help="whether to do dataparallel training", action="store_true")
    parser.add_argument("--ddp", help="whether to do DDP training", action="store_true")
    parser.add_argument('-n', '--nodes', default=1, type=int)
    parser.add_argument('-g', '--gpus', default=1, type=int,
                        help='number of gpus per node')
    parser.add_argument('-nr', '--nr', default=0, type=int,
                        help='ranking within the nodes')
    parser.add_argument("--port (highly recommended to specify it yourself, just pick a random number between 0 and 65530)", type=int, default=random.randint(0, 65530))
    # file names
    parser.add_argument("--log_file", help="log_file", type=str, default="")
    parser.add_argument("--mol_smi_filename", help="do not change", type=str,
                        default="50k_mol_smis.pickle")
    parser.add_argument("--smi_to_fp_dict_filename", help="do not change", type=str,
                        default="50k_mol_smi_to_sparse_fp_idx.pickle")
    parser.add_argument("--fp_to_smi_dict_filename", help="do not change", type=str,
                        default="50k_sparse_fp_idx_to_mol_smi.pickle")
    parser.add_argument("--mol_fps_filename", help="do not change", type=str,
                        default="50k_count_mol_fps.npz")
    parser.add_argument("--search_index_filename", help="do not change", type=str,
                        default="50k_cosine_count.bin")
    parser.add_argument("--mut_smis_filename", help="do not change", type=str,
                        default="50k_neg150_rad2_maxsize3_mutprodsmis.pickle")
    parser.add_argument("--rxn_smis_file_prefix", help="do not change", type=str,
                        default="50k_clean_rxnsmi_noreagent")
    parser.add_argument("--path_to_energies",
                        help="do not change (folder to store array of energy values for train & test data)", type=str)
    parser.add_argument("--proposals_csv_file_prefix",
                        help="do not change (CSV file containing proposals from retro models)", type=str)
    parser.add_argument("--vocab_file", help="vocab file for Transformer encoder", type=str, default=None)
    parser.add_argument("--precomp_file_prefix",
                        help="precomputed augmentation file prefix, expt.py will append f'_{phase}.npz' to the end",
                        type=str)
    parser.add_argument("--prob_file_prefix",
                        help="npy file of probabilities/scores from retro model",
                        type=str)
    parser.add_argument("--cache_suffix",
                        help="additional suffix for G2E cache files",
                        type=str, default=None)
    parser.add_argument("--prodfps_file_prefix",
                        help="npz file of product fingerprints",
                        type=str)
    parser.add_argument("--labels_file_prefix",
                        help="npy file of labels for mixture of experts",
                        type=str)
    # fingerprint params
    parser.add_argument("--representation", help="reaction representation", type=str, default="fingerprint")
    parser.add_argument("--rctfp_size", help="reactant fp size", type=int, default=16384)
    parser.add_argument("--prodfp_size", help="product fp size", type=int, default=16384)
    parser.add_argument("--difffp_size", help="product fp size", type=int, default=16384)
    parser.add_argument("--fp_radius", help="fp radius", type=int, default=3)
    parser.add_argument("--rxn_type", help="aggregation type", type=str, default="hybrid_all")
    parser.add_argument("--fp_type", help="fp type", type=str, default="count")
    # training params
    parser.add_argument("--loss_type", help="type of EBM loss ['log', 'hinge', 'ReLU']", type=str, default="log")
    parser.add_argument("--loss_margin", help="margin for hinge loss, provide multiple values for differential margin [top-2,top-3_to_5,top-5_to_10]etc.", 
                        type=float, nargs='+', default=[1])
    parser.add_argument("--batch_size", help="batch_size", type=int, default=128)
    parser.add_argument("--batch_size_eval", help="batch_size", type=int, default=128)
    parser.add_argument("--grad_clip", help="gradient clipping, 0 means no clipping", type=float, default=0)
    parser.add_argument("--minibatch_size", help="minibatch size for smiles (training), i.e. max # of proposal rxn_smi allowed per rxn", 
                        type=int, default=32)
    parser.add_argument("--minibatch_eval", help="minibatch size for smiles (valid/test), i.e. max # of proposal rxn_smi allowed per rxn, for finetuning", 
                        type=int, default=32)
    parser.add_argument("--max_seq_len", help="max sequence length for smiles representation", type=int, default=256)
    parser.add_argument("--optimizer", help="optimizer", type=str, default="Adam")
    parser.add_argument("--epochs", help="num. of epochs", type=int, default=30)
    parser.add_argument("--milestone_lr", help="Epochs for MultiStepLR", type=int, nargs='+')
    parser.add_argument("--milestone_gamma", help="Gamma for MultiStepLR", type=float, default=0.4)
    parser.add_argument("--learning_rate", help="learning rate", type=float, default=5e-3)
    parser.add_argument("--weight_decay", help="weight decay", type=float, default=0)
    parser.add_argument("--warmup_epochs", help="Num epochs to warm up LR (currently linearly) to given LR", 
                        type=int, default=0)
    parser.add_argument("--lr_floor_stop_training", help="whether to stop training once LR < --lr_floor", 
                        action="store_true", default=False)
    parser.add_argument("--lr_floor", help="LR below which training will stop", type=float, default=1e-7)
    parser.add_argument("--lr_min", help="minimum learning rate (CosineAnnealingWarmRestarts)", 
                        type=float, default=0)
    parser.add_argument("--new_lr", help="new learning rate after reloading checkpoint", 
                        type=float)
    parser.add_argument("--lr_cooldown", help="epochs to wait before resuming normal operation (ReduceLROnPlateau)",
                        type=int, default=0)
    parser.add_argument("--lr_scheduler",
                        help="learning rate schedule ['ReduceLROnPlateau', 'CosineAnnealingWarmRestarts', 'OneCycleLR']", 
                        type=str, default="ReduceLROnPlateau")
    parser.add_argument("--lr_scheduler_criteria",
                        help="criteria to reduce LR (ReduceLROnPlateau) ['loss', 'acc']", 
                        type=str, default='acc')
    parser.add_argument("--lr_scheduler_factor",
                        help="factor by which to reduce LR (ReduceLROnPlateau)", type=float, default=0.2)
    parser.add_argument("--lr_scheduler_patience",
                        help="num. of epochs with no improvement after which to reduce LR (ReduceLROnPlateau)",
                        type=int, default=0)
    parser.add_argument("--lr_scheduler_T_0",
                        help="num. of iters (epochs) for first restart (CosineAnnealingWarmRestarts)",
                        type=int, default=8)
    parser.add_argument("--lr_scheduler_epoch_offset",
                        help="num. of epochs to offset (CosineAnnealingWarmRestarts)",
                        type=int, default=0)
    parser.add_argument("--lr_scheduler_last_batch",
                        help="num. of batches to offset / has been computed (OneCycleLR)",
                        type=int, default=-1)
    parser.add_argument("--lr_scheduler_last_epoch",
                        help="last epoch, only applicable when reloading an existing checkpoint (MultiStepLR)",
                        type=int, default=-1)
    parser.add_argument("--early_stop", help="whether to use early stopping", action="store_true") # type=bool, default=True) 
    parser.add_argument("--early_stop_criteria",
                        help="criteria for early stopping ['loss', 'acc', top1_acc', 'top5_acc', 'top10_acc', 'top50_acc']",
                        type=str, default='top1_acc')
    parser.add_argument("--early_stop_patience",
                        help="num. of epochs tolerated without improvement in criteria before early stop",
                        type=int, default=2)
    parser.add_argument("--early_stop_min_delta",
                        help="min. improvement in criteria needed to not early stop", type=float, default=1e-4)
    parser.add_argument("--num_workers", help="num. of workers (0 to 8)", type=int, default=0)
    parser.add_argument("--checkpoint", help="whether to save model checkpoints", action="store_true") # type=bool, default=True)
    parser.add_argument("--checkpoint_every", help="to save model weights every N epochs", type=int, default=1)
    parser.add_argument("--random_seed", help="random seed", type=int, default=0)
    parser.add_argument("--pin_memory", 
                        help="whether to pin memory to speed up CPU to GPU transfer (will fail for certain cases, like TransformerEBM)", 
                        action="store_true")
    parser.add_argument("--drop_last",
                        help="Whether to drop last minibatch in train/valid/test dataloader",
                        action="store_true")
    parser.add_argument("--pos_weight", help="pos weight for BCEWithLogitsLoss (mixture model)", type=float, default=1)
    # model params, G2E/FF/S2E args
    parser.add_argument("--encoder_hidden_size", help="MPN/FFN/Transformer encoder_hidden_size(s)", 
                        type=int, nargs='+', default=256)
    parser.add_argument("--encoder_inner_hidden_size", help="MPN W_o hidden_size(s)", type=int, nargs='+', default=[320])
    parser.add_argument("--encoder_depth", help="MPN encoder_depth / Transformer num_layers", type=int, default=3)
    parser.add_argument("--encoder_num_heads", help="Transformer num_heads", type=int, default=4)
    parser.add_argument("--encoder_filter_size", help="Transformer filter_size", type=int, default=256)
    parser.add_argument("--encoder_embed_size", help="Transformer embedding size", type=int, default=64)
    parser.add_argument("--encoder_dropout", help="MPN/FFN/Transformer encoder dropout", type=float, default=0.05)
    parser.add_argument("--encoder_activation", help="MPN/FFN encoder activation", type=str, default="PReLU")
    parser.add_argument("--out_hidden_sizes", help="Output layer hidden sizes", type=int, nargs='+', default=[256])
    parser.add_argument("--out_activation", help="Output layer activation", type=str, default="PReLU")
    parser.add_argument("--out_dropout", help="Output layer dropout", type=float, default=0.2)
    parser.add_argument("--encoder_rnn_type", help="RNN type for graph encoder (gru/lstm)", type=str, default="gru")
    parser.add_argument("--atom_pool_type", help="Atom pooling method (sum/mean/attention)",
                        type=str, default="attention")
    parser.add_argument("--mol_pool_type", help="Molecule(s) pooling method (sum/mean)",
                        type=str, default="sum")
    parser.add_argument("--s2e_pool_type", help="Reaction pooling method for Transformer (mean/CLS)",
                        type=str, default="mean")                  
    parser.add_argument("--proj_hidden_sizes", help="Projection head hidden sizes", type=int, nargs='+', default=[256])
    parser.add_argument("--proj_activation", help="Projection head activation", type=str, default="PReLU")
    parser.add_argument("--proj_dropout", help="Projection head dropout", type=float, default=0.2)
    parser.add_argument("--attention_dropout", help="Attention dropout for Transformer", type=float, default=0.1)
    parser.add_argument("--preembed_size", help="Preembedding layer hidden size(s)", type=int, nargs='+')

    return parser.parse_args()

def setup_logger(args):
    dt = datetime.strftime(datetime.now(), "%y%m%d-%H%Mh")
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(f"./logs/{args.log_file}.{dt}")
    fh.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(sh)

def main_dist(
        gpu,
        args,
        model,
        model_name,
        model_args: dict,
        augmentations: dict,
        onthefly: Optional[bool] = False,
        debug: Optional[bool] = True,
        dataparallel: Optional[bool] = False,
        root: Optional[Union[str, bytes, os.PathLike]] = None,
        load_checkpoint: Optional[bool] = False,
        saved_optimizer: Optional[torch.optim.Optimizer] = None,
        saved_stats: Optional[dict] = None,
        begin_epoch: Optional[int] = None,
        vocab: Dict[str, int] = None,
    ):
    print('Initiating process group')
    # https://github.com/yangkky/distributed_tutorial/blob/master/ddp_tutorial.md
    rank = args.nr * args.gpus + gpu
    dist.init_process_group(
        backend='nccl',
        init_method=f'tcp://127.0.0.1:{args.port}', # for single-node, multi-GPU training # 'env://'
        world_size=args.world_size,
        rank=rank
    )
    
    if gpu == 0:
        setup_logger(args)

        logging.info("Logging args")
        logging.info(vars(args))
        
        if args.load_checkpoint:
            logging.info(f'Loading checkpoint of epoch {begin_epoch - 1}')

        logging.info("Logging model summary")
        logging.info(model)
        logging.info(f"\nModel #Params: {sum([x.nelement() for x in model.parameters()]) / 1000} k")
        logging.info(f'{model_args}')

    logging.info("Setting up DDP experiment")
    experiment = expt_dist.Experiment(
        gpu=gpu,
        args=args,
        model=model,
        model_name=args.model_name,
        model_args=model_args,
        augmentations=augmentations,
        onthefly=args.onthefly,
        load_checkpoint=args.load_checkpoint,
        dataparallel=False,
        saved_optimizer=saved_optimizer,
        saved_stats=saved_stats,
        begin_epoch=begin_epoch,
        debug=True,
        vocab=vocab,
        root=root
    )
    if not args.do_not_train and args.do_pretrain:
        logging.info("Start pretraining")
        experiment.train_distributed()

    if not args.do_not_train and args.do_finetune:
        assert args.proposals_csv_file_prefix is not None, f"Please provide --proposals_csv_file_prefix!"
        logging.info("Start finetuning")
        experiment.train_distributed()

    if args.do_test:
        logging.info("Start testing")
        if args.do_compute_graph_feat and experiment.train_loader is not None and not args.test_on_train:
            del experiment.train_loader # free up memory
            gc.collect()
            torch.cuda.empty_cache()
        experiment.test_distributed()

    if args.do_get_energies_and_acc and gpu == 0:
        # reload best checkpoint & use just 1 GPU to run
        args.ddp = False
        args.old_expt_name = args.expt_name
        # args.date_trained = str(args.checkpoint_folder)[-10:]
        args.load_checkpoint = True
        args.load_epoch = None # to load best checkpoint based on val top-1
        args.do_test = True
        args.do_not_train = True
        args.testing_best_ckpt = True
        # args.random_seed += 100 # just to get more diverse random examples of predictions, shouldn't affect anything else

        logging.info(f'Reloading expt: {args.expt_name}')
        main(args)
        
def main(args):
    model_utils.seed_everything(args.random_seed) # seed before even initializing model

    if args.torch_anomaly: # to debug nan loss
        torch.autograd.set_detect_anomaly(True)
    
    """train EBM from scratch"""
    if not args.ddp: # if ddp, we should only log from main process (0), not from all processes
        logging.info("Logging args")
        logging.info(vars(args))

    # hard-coded
    saved_model, saved_optimizer, saved_stats = None, None, None
    begin_epoch = 0
    augmentations = {} # deprecated
    vocab = {}
    model_args = {}
    if isinstance(args.encoder_hidden_size, list) and args.model_name.split('_')[0] == 'GraphEBM':
        assert len(args.encoder_hidden_size) == 1, 'MPN encoder_hidden_size must be a single integer!'
        args.encoder_hidden_size = args.encoder_hidden_size[0]

    if args.checkpoint_folder is None: # checkpoint_root & folder are usually None, unless you want to store checkpoints somewhere else
        args.checkpoint_folder = expt_utils.setup_paths("LOCAL", root=args.checkpoint_root)
    else:
        args.checkpoint_folder = Path(args.checkpoint_folder)
    
    if args.load_checkpoint:
        if not args.ddp:
            logging.info("Loading from checkpoint")
        old_checkpoint_folder = expt_utils.setup_paths( # checkpoint_root is usually None, unless you want to store checkpoints somewhere else
            # we DO require that checkpoints to be loaded are in the same root folder as checkpoints to be saved, for simplicity's sake
            "LOCAL", load_trained=True, date_trained=args.date_trained, root=args.checkpoint_root
        )
        saved_stats_filename = f'{args.model_name}_{args.old_expt_name}_stats.pkl'
        saved_model, saved_optimizer, saved_stats = expt_utils.load_model_opt_and_stats(
            args, saved_stats_filename, old_checkpoint_folder, args.optimizer,
            load_epoch=args.load_epoch 
        )
        if not args.ddp:
            logging.info(f"Saved model {args.model_name} loaded")
        model = saved_model
        model_args = saved_stats["model_args"]
        if args.load_epoch is not None:
            begin_epoch = int(args.load_epoch) + 1
        else:
            begin_epoch = int(saved_stats["best_epoch"]) + 1

        if args.vocab_file is not None:
            vocab = expt_utils.load_or_create_vocab(args)
    else:
        if not args.ddp:
            logging.info(f"Not loading from checkpoint, creating model {args.model_name}")
        if args.model_name == "FeedforwardEBM" or args.model_name == "FeedforwardTriple3indiv3prod1cos":
            model = FF.FeedforwardTriple3indiv3prod1cos(args)

        elif args.model_name == "GraphEBM":                     # Graph to energy
            model = G2E.G2E(args)
        elif args.model_name == "GraphEBM_Cross":               # Graph to energy, cross attention pool for r and p atoms
            model = G2E.G2ECross(args)
        elif args.model_name == "GraphEBM_projBoth":            # Graph to energy, project both reactants & products w/ dot product output
            model = G2E.G2E_projBoth(args)
        elif args.model_name == "GraphEBM_projBoth_FFout":      # Graph to energy, project both reactants & products, concat then feedforward output
            model = G2E.G2E_projBoth_FFout(args)
        elif args.model_name == "GraphEBM_sep_projBoth_FFout":  # Graph to energy, separate MPN encoders + projections, feedforward output
            model = G2E.G2E_sep_projBoth_FFout(args)
            
        elif args.model_name == "TransformerEBM":               # Sequence to energy
            assert args.vocab_file is not None, "Please provide precomputed --vocab_file!"
            vocab = expt_utils.load_or_create_vocab(args)
            model = S2E.S2E(args, vocab)
        else:
            raise ValueError(f"Model {args.model_name} not supported!")

        if not args.ddp:
            logging.info(f"Model {args.model_name} created")    # model.model_repr wont work with DataParallel

    if not args.ddp:
        logging.info("Logging model summary")
        logging.info(model)
        logging.info(f"\nModel #Params: {sum([x.nelement() for x in model.parameters()]) / 1000} k")
        logging.info(f'Model args: {model_args}')
    
    if args.ddp:
        args.world_size = args.gpus * args.nodes
        args.port = args.port or random.randint(0, 65535)
        mp.spawn(main_dist,
            nprocs=args.gpus, 
            args=(
                args,
                model,
                args.model_name,
                model_args,
                augmentations,
                args.onthefly,
                True, # debug
                False, # dataparallel
                args.root,
                args.load_checkpoint,
                saved_optimizer,
                saved_stats,
                begin_epoch,
                vocab
                )
            )
    else:
        if torch.cuda.device_count() > 1 and args.dataparallel:
            logging.info(f'Using {torch.cuda.device_count()} GPUs!!!')
            dataparallel = True
            model = nn.DataParallel(model)
        elif torch.cuda.device_count() > 1:
            logging.info(f'Using only 1 GPU out of {torch.cuda.device_count()} GPUs! \
                You should either do --ddp (recommended) or --dataparallel training!')
            dataparallel = False
        elif torch.cuda.is_available():
            logging.info(f"Using 1 GPU out of 1 GPU! Where're the rest?")
            dataparallel = False
        else:
            logging.info(f'Using CPU! Warning! Training will be slowwwwww')
            dataparallel = False

        logging.info("Setting up experiment")
        experiment = expt.Experiment(
            args=args,
            model=model,
            model_name=args.model_name,
            model_args=model_args,
            augmentations=augmentations,
            onthefly=args.onthefly,
            load_checkpoint=args.load_checkpoint,
            saved_optimizer=saved_optimizer,
            saved_stats=saved_stats,
            begin_epoch=begin_epoch,
            debug=True,
            vocab=vocab,
            gpu=None, # not doing DDP
            root=args.root
        )
        if not args.do_not_train and args.do_pretrain:
            logging.info("Start pretraining")
            experiment.train()

        if not args.do_not_train and args.do_finetune:
            assert args.proposals_csv_file_prefix is not None, f"Please provide --proposals_csv_file_prefix!"
            logging.info("Start finetuning")
            experiment.train()

        if args.do_test:
            if not args.test_on_train:
                del experiment.train_loader # free up memory
                gc.collect()
                torch.cuda.empty_cache()
            logging.info("Start testing")
            experiment.test()

        if args.do_get_energies_and_acc and not args.testing_best_ckpt:
            # reload best checkpoint & use just 1 GPU to run
            args.ddp = False
            if args.old_expt_name is None: # user did not provide, means we are training from scratch, not loading a checkpoint
                args.old_expt_name = args.expt_name
            # args.date_trained = str(args.checkpoint_folder)[-10:]
            args.load_checkpoint = True
            args.load_epoch = None # to load best checkpoint based on val top-1
            args.do_test = True
            args.do_not_train = True
            args.testing_best_ckpt = True # turn flag on

            logging.info(f'Reloading expt: {args.expt_name}')
            main(args)

        elif args.do_get_energies_and_acc and args.testing_best_ckpt:
            phases_to_eval = ['train', 'valid', 'test'] if args.test_on_train else ['valid', 'test']
            for phase in phases_to_eval:
                experiment.get_energies_and_loss(
                    phase=phase, save_energies=True, path_to_energies=args.path_to_energies
                )

            # just print accuracies to compare experiments
            for phase in phases_to_eval:
                logging.info(f"\nGetting {phase} accuracies")
                message = f"{args.expt_name}\n"
                for k in [1, 3, 5, 10, 20, 50]:
                    message = experiment.get_topk_acc(phase=phase, k=k, message=message)
                try:
                    send_message(message)
                except Exception as e:
                    pass

            # full accuracies
            for phase in phases_to_eval:
                logging.info(f"\nGetting {phase} accuracies")
                for k in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100, 150, 200]:
                    experiment.get_topk_acc(phase=phase, k=k)

if __name__ == "__main__":
    args = parse_args()

    # logger setup
    RDLogger.DisableLog("rdApp.warning")
    os.makedirs("./logs", exist_ok=True)
    if not args.ddp: # if doing ddp training, must spawn logger inside just 1 child process (rank == 0)
        setup_logger(args)
    main(args)
