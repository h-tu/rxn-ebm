import argparse
import logging
import os
import sys
import torch

import gc
gc.enable() 

from datetime import datetime
from rdkit import RDLogger
from rxnebm.data import dataset
from rxnebm.experiment import expt, expt_utils
from rxnebm.model import FF 

torch.backends.cudnn.benchmark = True

def parse_args():
    parser = argparse.ArgumentParser("finetuneEBM.py")
    # file names
    parser.add_argument("--log_file", help="log_file", type=str, default="")
    parser.add_argument("--path_to_energies", help="do not change (folder to store array of energy values for train & test data)", type=str)
    parser.add_argument("--proposals_csv_file_prefix", help="do not change (CSV file containing proposals from retro models)", 
                    type=str, default='retrosim_200maxtest_200maxprec')
    # fingerprint params
    parser.add_argument("--representation", help="reaction representation", type=str, default="fingerprint")
    # training params 
    parser.add_argument("--model_name", help="model name", type=str, default="FeedforwardEBM")
    parser.add_argument("--old_expt_name", help="old experiment name", type=str, default="")
    parser.add_argument("--expt_name", help="experiment name", type=str, default="")
    parser.add_argument("--precomp_file_prefix",
                        help="precomputed rxn_fp file prefix, expt.py will append f'_{phase}.npz' to the end",
                        type=str, default="")
    parser.add_argument("--date_trained", help="date trained (DD_MM_YYYY)", type=str, default="02_11_2020")
    parser.add_argument("--checkpoint_folder", help="checkpoint folder",
                        type=str, default=expt_utils.setup_paths("LOCAL"))
    parser.add_argument("--batch_size", help="batch_size", type=int, default=2048)
    parser.add_argument("--optimizer", help="optimizer", type=str, default="Adam")
    parser.add_argument("--epochs", help="num. of epochs", type=int, default=30)
    parser.add_argument("--learning_rate", help="learning rate", type=float, default=5e-3)
    parser.add_argument("--lr_scheduler", help="learning rate schedule", type=str, default="ReduceLROnPlateau")
    parser.add_argument("--lr_scheduler_criteria", help="criteria for learning rate scheduler ['loss', 'acc']", type=str, default='acc')
    parser.add_argument("--lr_scheduler_factor", help="factor by which learning rate will be reduced", type=float, default=0.3)
    parser.add_argument("--lr_scheduler_patience", help="num. of epochs with no improvement after which learning rate will be reduced", type=int, default=1)
    parser.add_argument("--early_stop", help="whether to use early stopping", action="store_true") # type=bool, default=True) 
    parser.add_argument("--early_stop_criteria", help="criteria for early stopping ['loss', 'top1_acc', 'top5_acc', 'top10_acc', 'top50_acc']", 
                        type=str, default='top1_acc')
    parser.add_argument("--early_stop_patience", help="num. of epochs tolerated without improvement in criteria before early stop", type=int, default=2)
    parser.add_argument("--early_stop_min_delta", help="min. improvement in criteria needed to not early stop", type=float, default=1e-4) # acc is in percentage from 0 to 100
    parser.add_argument("--num_workers", help="num. of workers (0 to 8)", type=int, default=0)
    parser.add_argument("--checkpoint", help="whether to save model checkpoints", action="store_true") # type=bool, default=True) 
    parser.add_argument("--random_seed", help="random seed", type=int, default=0)
    # model params, for now just use model_args with different models

    return parser.parse_args()


def finetune(args):
    """finetune a trained EBM"""
    logging.info("Logging args")
    logging.info(vars(args))
    logging.info("Setting up model and experiment")

    old_checkpoint_folder = expt_utils.setup_paths(
        "LOCAL", load_trained=True, date_trained=args.date_trained
    )
    saved_stats_filename = f'{args.model_name}_{args.old_expt_name}_stats.pkl'
    saved_model, saved_optimizer, saved_stats = expt_utils.load_model_opt_and_stats(
        saved_stats_filename, old_checkpoint_folder, args.model_name, args.optimizer
    )
    logging.info(f"Saved model {saved_model.model_repr} loaded, logging model summary")
    logging.info(saved_model)
    logging.info(f"\nModel #Params: {sum([x.nelement() for x in saved_model.parameters()]) / 1000} k")

    logging.info("Updating args with fp_args")
    for k, v in saved_stats["fp_args"]:
        setattr(args, k, v)

    experiment = expt.Experiment(
        args=args,
        model=saved_model,
        model_args=saved_stats["model_args"],
        augmentations=saved_stats["augmentations"],
        load_checkpoint=True, 
        saved_optimizer=saved_optimizer,
        saved_stats=saved_stats,
        saved_stats_filename=saved_stats_filename,
        begin_epoch=0,
        debug=True
    )

    logging.info("Start finetuning")
    experiment.train()
    experiment.test()

    _, _ = experiment.get_energies_and_loss(phase="train", save_energies=True, path_to_energies=args.path_to_energies)
    _, _, _ = experiment.get_energies_and_loss(phase="val", finetune=True, save_energies=True, path_to_energies=args.path_to_energies)
    _, _, _ = experiment.get_energies_and_loss(phase="test", finetune=True, save_energies=True, path_to_energies=args.path_to_energies)
    logging.info('\nGetting train accuracies')
    for k in [1, 2, 3, 5, 10, 20, 50, 100]:
        experiment.get_topk_acc(phase="train", k=k)
    logging.info('\nGetting val accuracies')
    for k in [1, 2, 3, 5, 10, 20, 50, 100]:
        experiment.get_topk_acc(phase="val", finetune=True, k=k)
    logging.info('\nGetting test accuracies')
    for k in [1, 2, 3, 5, 10, 20, 50, 100]:
        experiment.get_topk_acc(phase="test", finetune=True, k=k)

if __name__ == "__main__":
    args = parse_args()

    # logger setup
    RDLogger.DisableLog("rdApp.warning")

    os.makedirs("./logs", exist_ok=True)
    dt = datetime.strftime(datetime.now(), "%y%m%d-%H%Mh")

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    # logger.propagate = False
    fh = logging.FileHandler(f"./logs/{args.log_file}.{dt}")
    fh.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(sh)

    finetune(args)
