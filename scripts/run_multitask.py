import music21
import torch

from fastai.distributed import *
from fastai.callbacks import SaveModelCallback
try: from apex.optimizers import FusedAdam
except: from torch.optim import Adam as FusedAdam

import numpy as np

import sys
sys.path.insert(0, '..')

from musicautobot.music_transformer import *
from musicautobot.multitask_transformer import *
from musicautobot.utils.stacked_dataloader import StackedDataBunch

import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--path', type=str, default='../data/numpy/')
parser.add_argument('--data_file', type=str, default='musicitem_data_save.pkl')
parser.add_argument('--s2s_data_file', type=str, default='multiitem_data_save.pkl')
parser.add_argument('--save', type=str, default='first_run')
parser.add_argument('--load', type=str, default=None)
parser.add_argument("--local_rank", type=int, default=0)
parser.add_argument("--batch_size", type=int, default=4)
parser.add_argument("--bptt", type=int, default=1024)
parser.add_argument('--half', action='store_true', help='Use half precision')
parser.add_argument('--lamb', action='store_true', help='Use lamb optimizer')
parser.add_argument('--wd', type=float, default=1e-3, help='weight decay for adam')
parser.add_argument('--epochs', type=int, default=5, help='num epochs')
parser.add_argument('--lr', type=float, default=1e-3, help='learning rate')
parser.add_argument('--div_factor', type=int, default=10, help='learning rate div factor')
parser.add_argument('--save_every', action='store_true', help='Save every epoch')
parser.add_argument('--config', type=str, default='multitask_config', help='serve.py config name')
parser.add_argument('--no_transpose', action='store_true', help='No transpose data augmentation')
parser.add_argument('--data_parallel', action='store_true', help='DataParallel instead of DDP')
parser.add_argument('--s2s_mask_window', type=int, default=1,
                    help='Starting mask window size for sequence2sequence task. Basically teacher forcing')

args = parser.parse_args()
args.path = Path(args.path)


if args.local_rank != 0:
    f = open('/dev/null', 'w')
    sys.stdout = f

# if is_distributed:
#     torch.cuda.set_device(args.local_rank)
#     torch.distributed.init_process_group(backend='nccl', init_method='env://')
is_distributed = num_distrib() > 0
setup_distrib(args.local_rank)

path = Path(args.path)

from musicautobot import config
config = getattr(config, args.config)()

if args.no_transpose: config['transpose_range'] = None

datasets = []


data = load_data(args.path, args.data_file, 
                 bs=args.batch_size, bptt=args.bptt, transpose_range=config['transpose_range'],
                 encode_position=True, dl_tfms=mask_lm_tfm)

datasets.append(data)

s2s_data = load_data(args.path, args.s2s_data_file, 
                    bs=args.batch_size//4, bptt=args.bptt, transpose_range=config['transpose_range'],
                     preloader_cls=S2SPreloader, dl_tfms=melody_chord_tfm)

datasets.append(s2s_data)

combined_data = StackedDataBunch(datasets)

# Load Optimizer
eps = 1e-3 if args.half else 1e-6
opt_func = partial(FusedAdam, betas=(0.9,0.99), eps=eps)
if args.lamb:
    from musicautobot.utils.lamb import Lamb
    opt_func = partial(Lamb, eps=eps)
    
# Load Learner
learn = multitask_model_learner(combined_data, config.copy(), opt_func=opt_func)
if not args.half: learn.clip_grad(0.5)

if args.load:
    state = torch.load(path/args.load, map_location='cpu')
    get_model(learn.model).load_state_dict(state['model'], strict=False)
    learn.model.cuda()
if args.save:
    save_path = path/learn.model_dir/args.save
    save_path.parent.mkdir(parents=True, exist_ok=True)
if args.half: learn = learn.to_fp16(clip=0.5, dynamic=True, max_scale=2**18)
if is_distributed: learn = learn.to_distributed(args.local_rank, cache_dir=path/'dist_logs')
if args.data_parallel: learn = learn.to_parallel()
if args.local_rank == 0: learn.callbacks.append(SaveModelCallback(learn, name=f'{args.save}_best'))

learn.fit_one_cycle(args.epochs, args.lr, div_factor=args.div_factor, pct_start=.3, final_div=50, wd=args.wd)

if args.local_rank == 0: learn.save(f'{args.save}')
