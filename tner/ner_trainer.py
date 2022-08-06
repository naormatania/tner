import string
import os
import json
import logging
import random
import gc
from glob import glob
from os.path import join as pj
from typing import List
from itertools import product
from distutils.dir_util import copy_tree

import torch
import transformers

from .get_dataset import get_dataset, concat_dataset
from .model import TransformersNER

__all__ = ('GridSearcher', 'Trainer')


def load_json(_file):
    with open(_file, 'r') as f:
        return json.load(f)


def write_json(_obj, _file):
    with open(_file, 'w') as f:
        json.dump(_obj, f)


def get_random_string(length: int = 6, exclude: List = None):
    tmp = ''.join(random.choice(string.ascii_lowercase) for _ in range(length))
    if exclude:
        while tmp in exclude:
            tmp = ''.join(random.choice(string.ascii_lowercase) for _ in range(length))
    return tmp


class Trainer:

    def __init__(self,
                 checkpoint_dir: str,
                 data_split: str = '2020.train',
                 model: str = 'xlm-roberta-large',
                 crf: bool = False,
                 max_length: int = 128,
                 epoch: int = 10,
                 batch_size: int = 128,
                 lr: float = 1e-4,
                 random_seed: int = 42,
                 gradient_accumulation_steps: int = 4,
                 weight_decay: float = 1e-7,
                 lr_warmup_step_ratio: int = None,
                 max_grad_norm: float = None,
                 disable_log: bool = False,
                 use_auth_token: bool = False,
                 config_file: str = 'trainer_config.json'):
        self.checkpoint_dir = checkpoint_dir
        # load model
        self.model = None
        self.current_epoch = 0
        for e in sorted([int(i.split('epoch_')[-1]) for i in glob(f'{self.checkpoint_dir}/epoch_*')], reverse=True):
            if not os.path.exists(f"{self.checkpoint_dir}/optimizers/optimizer.{e}.pt"):
                continue
            try:
                path = pj(self.checkpoint_dir, f'epoch_{e}')
                logging.info(f'load checkpoint from {path}')
                self.config = load_json(pj(self.checkpoint_dir, config_file))
                self.model = TransformersNER(path, self.config['max_length'], self.config['crf'],
                                             use_auth_token=use_auth_token)
                self.current_epoch = e
                assert self.current_epoch <= self.config['epoch'], 'model training is over'
            except Exception:
                logging.exception('error at loading checkpoint')
        if self.model is None:
            self.config = dict(
                data_split=data_split, model=model, crf=crf, max_length=max_length, epoch=epoch, batch_size=batch_size,
                lr=lr, random_seed=random_seed, gradient_accumulation_steps=gradient_accumulation_steps,
                weight_decay=weight_decay, lr_warmup_step_ratio=lr_warmup_step_ratio, max_grad_norm=max_grad_norm)
            self.model = TransformersNER(self.config['model'], self.config['max_length'], self.config['crf'],
                                         use_auth_token=use_auth_token)
            os.makedirs(self.checkpoint_dir, exist_ok=True)
            with open(pj(self.checkpoint_dir, config_file), 'w') as f:
                json.dump(self.config, f)

        logging.info('hyperparameters')
        for k, v in self.config.items():
            logging.info(f'\t * {k}: {v}')

        random.seed(self.config['random_seed'])
        torch.manual_seed(self.config['random_seed'])
        # get data
        self.dataset = get_dataset(self.config['data_split'])
        self.step_per_epoch = int(
            len(self.dataset['data']) / self.config['batch_size'] / self.config['gradient_accumulation_steps']
        )

        if not disable_log:
            # add file handler
            logger = logging.getLogger()
            file_handler = logging.FileHandler(pj(self.checkpoint_dir, 'training.log'))
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-8s %(message)s'))
            logger.addHandler(file_handler)
        self.scheduler = None
        self.optimizer = None

    def save(self, current_epoch):
        # save model
        save_dir = pj(self.checkpoint_dir, f'epoch_{current_epoch + 1}')
        os.makedirs(save_dir, exist_ok=True)
        logging.info(f'model saving at {save_dir}')
        self.model.save(save_dir)
        # save optimizer
        save_dir_opt = pj(self.checkpoint_dir, 'optimizers', f'optimizer.{current_epoch + 1}.pt')
        os.makedirs(os.path.dirname(save_dir_opt), exist_ok=True)
        # Fix the memory error
        logging.info(f'optimizer saving at {save_dir_opt}')
        if self.scheduler is not None:
            torch.save({
                'optimizer_state_dict': self.optimizer.state_dict(),
                'scheduler_state_dict': self.scheduler.state_dict(),
            }, save_dir_opt)
        else:
            torch.save({'optimizer_state_dict': self.optimizer.state_dict()}, save_dir_opt)
        logging.info('remove old optimizer files')
        if os.path.exists(pj(self.checkpoint_dir, 'optimizers', f'optimizer.{current_epoch}.pt')):
            os.remove(pj(self.checkpoint_dir, 'optimizers', f'optimizer.{current_epoch}.pt'))

    def train(self, epoch_save: int = None, epoch_partial: int = None, optimizer_on_cpu: bool = False):
        logging.info('dataset preprocessing')
        self.model.train()
        self.setup_optimizer(optimizer_on_cpu)
        assert self.current_epoch != self.config['epoch'], 'training is over'
        cache = pj(
            "cache",
            "encoded",
            f"{self.config['model']}.{self.config['max_length']}.{self.config['crf']}.{self.config['data_split']}.pkl")
        loader = self.model.get_data_loader(inputs=self.dataset['data'], labels=self.dataset['label'],
                                            batch_size=self.config['batch_size'], shuffle=True, drop_last=True,
                                            cache_file_feature=cache)
        logging.info('start model training')
        interval = 50
        for e in range(self.current_epoch, self.config['epoch']):  # loop over the epoch
            total_loss = []
            self.optimizer.zero_grad()
            for n, encode in enumerate(loader):
                loss = self.model.encode_to_loss(encode)
                loss.backward()
                if self.config['max_grad_norm'] is not None:
                    torch.nn.utils.clip_grad_norm_(self.model.model.parameters(), self.config['max_grad_norm'])
                total_loss.append(loss.cpu().item())
                if (n + 1) % self.config['gradient_accumulation_steps'] != 0:
                    continue
                # optimizer update
                self.optimizer.step()
                if self.scheduler is not None:
                    self.scheduler.step()
                self.optimizer.zero_grad()
                if len(total_loss) % interval == 0:
                    _tmp_loss = round(sum(total_loss) / len(total_loss), 2)
                    lr = self.optimizer.param_groups[0]['lr']
                    logging.info(f"\t * global step {len(total_loss)}: loss: {_tmp_loss}, lr: {lr}")
            self.optimizer.zero_grad()
            _tmp_loss = round(sum(total_loss) / len(total_loss), 2)
            lr = self.optimizer.param_groups[0]['lr']
            logging.info(f"[epoch {e}/{self.config['epoch']}] average loss: {_tmp_loss}, lr: {lr}")
            if epoch_save is not None and (e + 1) % epoch_save == 0 and (e + 1) != 0:
                self.save(e)
            if epoch_partial is not None and (e + 1) == epoch_partial:
                break
        self.save(e)
        logging.info(f'complete training: model ckpt was saved at {self.checkpoint_dir}')

    def setup_optimizer(self, optimizer_on_cpu):
        # optimizer
        if self.config['weight_decay'] is not None and self.config['weight_decay'] != 0:
            no_decay = ["bias", "LayerNorm.weight"]
            optimizer_grouped_parameters = [
                {"params": [p for n, p in self.model.model.named_parameters() if not any(nd in n for nd in no_decay)],
                 "weight_decay": self.config['weight_decay']},
                {"params": [p for n, p in self.model.model.named_parameters() if any(nd in n for nd in no_decay)],
                 "weight_decay": 0.0}]
            self.optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=self.config['lr'])
        else:
            self.optimizer = torch.optim.AdamW(self.model.model.parameters(), lr=self.config['lr'])
        if self.config['lr_warmup_step_ratio'] is not None:
            total_step = self.step_per_epoch * self.config['epoch']
            num_warmup_steps = int(total_step * self.config['lr_warmup_step_ratio'])
            self.scheduler = transformers.get_linear_schedule_with_warmup(
                self.optimizer, num_warmup_steps=num_warmup_steps, num_training_steps=total_step)

        # resume fine-tuning
        if self.current_epoch is not None and self.current_epoch != 0:
            path = pj(self.checkpoint_dir, "optimizers", f'optimizer.{self.current_epoch}.pt')
            logging.info(f'load optimizer from {path}')
            device = 'cpu' if optimizer_on_cpu == 1 else self.model.device
            logging.info(f'optimizer is loading on {device}')
            stats = torch.load(path, map_location=torch.device(device))
            self.optimizer.load_state_dict(stats['optimizer_state_dict'])
            if self.scheduler is not None:
                logging.info(f'load scheduler from {path}')
                self.scheduler.load_state_dict(stats['scheduler_state_dict'])
            del stats
            gc.collect()


class GridSearcher:
    """ Grid search (epoch, batch, lr, random_seed, label_smoothing) """

    def __init__(self,
                 checkpoint_dir: str,
                 data_train: str = '2020.train',
                 data_dev: str = '2020.dev',
                 model: str = 'xlm-roberta-large',
                 epoch: int = 10,
                 epoch_partial: int = 5,
                 n_max_config: int = 5,
                 max_length: int = 128,
                 max_length_eval: int = 128,
                 batch_size: int = 32,
                 batch_size_eval: int = 16,
                 gradient_accumulation_steps: List or int = 1,
                 crf: List or bool = True,
                 lr: List or float = 1e-4,
                 weight_decay: List or float = None,
                 random_seed: List or int = 0,
                 lr_warmup_step_ratio: List or int = None,
                 max_grad_norm: List or float = None,
                 use_auth_token: bool = False):
        # evaluation configs
        self.eval_config = {'max_length_eval': max_length_eval, 'metric': 'micro/f1', 'data_split': data_dev}
        # static configs
        self.static_config = {'data_split': data_train, 'model': model, 'batch_size': batch_size,
                              'epoch': epoch, 'max_length': max_length}
        # dynamic config
        self.checkpoint_dir = checkpoint_dir
        self.epoch = epoch
        self.epoch_partial = epoch_partial
        self.batch_size_eval = batch_size_eval
        self.n_max_config = n_max_config
        self.use_auth_token = use_auth_token

        def to_list(_val):
            if type(_val) != list:
                return [_val]
            assert len(_val) == len(set(_val)), _val
            if None in _val:
                _val.pop(_val.index(None))
                return [None] + sorted(_val, reverse=True)
            return sorted(_val, reverse=True)

        self.dynamic_config = {
            'lr': to_list(lr),
            'crf': to_list(crf),
            'random_seed': to_list(random_seed),
            'weight_decay': to_list(weight_decay),
            'lr_warmup_step_ratio': to_list(lr_warmup_step_ratio),
            'max_grad_norm': to_list(max_grad_norm),
            'gradient_accumulation_steps': to_list(gradient_accumulation_steps)
        }

        self.all_dynamic_configs = list(product(
            self.dynamic_config['lr'],
            self.dynamic_config['crf'],
            self.dynamic_config['random_seed'],
            self.dynamic_config['weight_decay'],
            self.dynamic_config['lr_warmup_step_ratio'],
            self.dynamic_config['max_grad_norm'],
            self.dynamic_config['gradient_accumulation_steps'],
        ))

    def run(self):
        os.makedirs(self.checkpoint_dir, exist_ok=True)
        # sanity check
        for _f, c in zip(['config_static', 'config_dynamic.json', 'config_eval.json'],
                         [self.static_config, self.dynamic_config, self.eval_config]):
            if os.path.exists(f'{self.checkpoint_dir}/{c}'):
                tmp = load_json(f'{self.checkpoint_dir}/{c}')
                tmp_v = [tmp[k] for k in sorted(tmp.keys())]
                _tmp_v = [c[k] for k in sorted(tmp.keys())]
                assert tmp_v == _tmp_v, f'{str(tmp_v)}\n not matched \n{str(_tmp_v)}'
        write_json(self.static_config, pj(self.checkpoint_dir, 'config_static.json'))
        write_json(self.dynamic_config, pj(self.checkpoint_dir, 'config_dynamic.json'))
        write_json(self.eval_config, pj(self.checkpoint_dir, 'config_eval.json'))

        # add file handler
        logger = logging.getLogger()
        file_handler = logging.FileHandler(pj(self.checkpoint_dir, 'grid_search.log'))
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-8s %(message)s'))
        logger.addHandler(file_handler)
        logging.info(f'INITIALIZE GRID SEARCHER: {len(self.all_dynamic_configs)} configs to try')
        cache_prefix = pj("cache", "encoded", f"{self.static_config['model']}.{self.static_config['max_length']}.dev")

        ###########
        # 1st RUN #
        ###########
        checkpoints = []
        ckpt_exist = {}
        for trainer_config in glob(pj(self.checkpoint_dir, 'model_*', 'trainer_config.json')):
            ckpt_exist[os.path.dirname(trainer_config)] = load_json(trainer_config)
        for n, dynamic_config in enumerate(self.all_dynamic_configs):
            logging.info(f'## 1st RUN: Configuration {n}/{len(self.all_dynamic_configs)} ##')
            config = self.static_config.copy()
            tmp_dynamic_config = {'lr': dynamic_config[0], 'crf': dynamic_config[1], 'random_seed': dynamic_config[2],
                                  'weight_decay': dynamic_config[3], 'lr_warmup_step_ratio': dynamic_config[4],
                                  'max_grad_norm': dynamic_config[5], 'gradient_accumulation_steps': dynamic_config[6]}
            config.update(tmp_dynamic_config)
            ex_dynamic_config = [(k_, [v[k] for k in sorted(tmp_dynamic_config.keys())]) for k_, v in ckpt_exist.items()]
            tmp_dynamic_config = [tmp_dynamic_config[k] for k in sorted(tmp_dynamic_config.keys())]
            duplicated_ckpt = [k for k, v in ex_dynamic_config if v == tmp_dynamic_config]

            if len(duplicated_ckpt) == 1:
                checkpoint_dir = duplicated_ckpt[0]
            elif len(duplicated_ckpt) == 0:
                ckpt_name_exist = [os.path.basename(k).replace('model_', '') for k in ckpt_exist.keys()]
                ckpt_name_made = [os.path.basename(c).replace('model_', '') for c in checkpoints]
                model_ckpt = get_random_string(exclude=ckpt_name_exist + ckpt_name_made)
                checkpoint_dir = pj(self.checkpoint_dir, f'model_{model_ckpt}')
            else:
                raise ValueError(f'duplicated checkpoints are found: \n {duplicated_ckpt}')

            if not os.path.exists(pj(checkpoint_dir, f'epoch_{self.epoch_partial}')):
                trainer = Trainer(checkpoint_dir=checkpoint_dir, disable_log=True, use_auth_token=self.use_auth_token,
                                  **config)
                trainer.train(epoch_partial=self.epoch_partial, epoch_save=1)
            checkpoints.append(checkpoint_dir)

        path_to_metric_1st = pj(self.checkpoint_dir, 'metric.1st.json')
        metrics = {}
        for n, checkpoint_dir in enumerate(checkpoints):
            logging.info(f'## 1st RUN (EVAL): Configuration {n}/{len(checkpoints)} ##')
            checkpoint_dir_model = pj(checkpoint_dir, f'epoch_{self.epoch_partial}')
            metric, tmp_metric = self.eval_single_model(checkpoint_dir_model, cache_prefix)
            write_json(metric, pj(checkpoint_dir_model, "eval", "metric.json"))
            metrics[checkpoint_dir_model] = tmp_metric[self.eval_config['metric']]
        metrics = sorted(metrics.items(), key=lambda x: x[1], reverse=True)
        write_json(metrics, path_to_metric_1st)

        logging.info('1st RUN RESULTS')
        for n, (k, v) in enumerate(metrics):
            logging.info(f'\t * rank: {n} | metric: {round(v, 3)} | model: {k} |')

        if self.epoch_partial == self.epoch:
            logging.info('No 2nd phase as epoch_partial == epoch')
            return

        ###########
        # 2nd RUN #
        ###########
        metrics = metrics[:min(len(metrics), self.n_max_config)]
        checkpoints = []
        for n, (checkpoint_dir_model, _metric) in enumerate(metrics):
            logging.info(f'## 2nd RUN: Configuration {n}/{len(metrics)}: {_metric}')
            model_ckpt = os.path.dirname(checkpoint_dir_model)
            if not os.path.exists(pj(model_ckpt, f'epoch_{self.epoch}')):
                trainer = Trainer(checkpoint_dir=model_ckpt, disable_log=True, use_auth_token=self.use_auth_token)
                trainer.train(epoch_save=1)
            checkpoints.append(model_ckpt)
        metrics = {}
        for n, checkpoint_dir in enumerate(checkpoints):
            logging.info(f'## 2nd RUN (EVAL): Configuration {n}/{len(checkpoints)} ##')
            for checkpoint_dir_model in sorted(glob(pj(checkpoint_dir, 'epoch_*'))):
                metric, tmp_metric = self.eval_single_model(checkpoint_dir_model, cache_prefix)
                write_json(metric, pj(checkpoint_dir_model, 'eval', 'metric.json'))
                metrics[checkpoint_dir_model] = tmp_metric[self.eval_config['metric']]
        metrics = sorted(metrics.items(), key=lambda x: x[1], reverse=True)
        logging.info(f'2nd RUN RESULTS: \n{str(metrics)}')
        for n, (k, v) in enumerate(metrics):
            logging.info(f'\t * rank: {n} | metric: {round(v, 3)} | model: {k} |')
        write_json(metrics, pj(self.checkpoint_dir, 'metric.2nd.json'))

        best_model_ckpt, best_metric_score = metrics[0]
        epoch = int(best_model_ckpt.split(os.path.sep)[-1].replace('epoch_', ''))
        best_model_dir = os.path.dirname(best_model_ckpt)
        with open(pj(best_model_dir, 'trainer_config.json')) as f:
            config = json.load(f)

        if epoch == self.static_config['epoch']:
            ###########
            # 3rd RUN #
            ###########
            logging.info(f'## 3rd RUN: target model: {best_model_dir} (metric: {best_metric_score}) ##')
            metrics = [[epoch, best_metric_score]]
            while True:
                epoch += 1
                logging.info(f'## 3rd RUN (TRAIN): epoch {epoch} ##')
                config['epoch'] = epoch
                with open(pj(best_model_dir, 'trainer_config.additional_training.json'), 'w') as f:
                    json.dump(config, f)
                checkpoint_dir_model = pj(best_model_dir, f'epoch_{epoch}')
                if not os.path.exists(checkpoint_dir_model):
                    trainer = Trainer(
                        checkpoint_dir=best_model_dir, config_file='trainer_config.additional_training.json',
                        use_auth_token=self.use_auth_token, disable_log=True)
                    trainer.train(epoch_save=1)
                logging.info(f'## 3rd RUN (EVAL): epoch {epoch} ##')

                metric, tmp_metric = self.eval_single_model(checkpoint_dir_model, cache_prefix)
                tmp_metric_score = tmp_metric[self.eval_config['metric']]
                metrics.append([epoch, tmp_metric_score])
                logging.info(f'\t tmp metric: {tmp_metric_score}')
                if best_metric_score > tmp_metric_score:
                    logging.info('\t finish 3rd phase (no improvement)')
                    break
                else:
                    logging.info(f'\t tmp metric improved the best model ({best_metric_score} --> {tmp_metric_score})')
                    best_metric_score = tmp_metric_score
            logging.info(f'3rd RUN RESULTS: {best_model_dir}')
            for k, v in metrics:
                logging.info(f'\t epoch {k}: {v}')
            write_json(metrics, pj(self.checkpoint_dir, 'metric.3rd.json'))
            config['epoch'] = epoch - 1
            best_model_ckpt = f"{best_model_ckpt.split('epoch_')[0]}epoch_{config['epoch']}"

        copy_tree(best_model_ckpt, pj(self.checkpoint_dir, 'best_model'))
        with open(pj(self.checkpoint_dir, 'best_model', 'trainer_config.json'), 'w') as f:
            json.dump(config, f)

    def eval_single_model(self, checkpoint_dir_model, cache_prefix, use_auth_token: bool = False):
        metric = {}
        if os.path.exists(pj(checkpoint_dir_model, 'eval', 'metric.json')):
            metric = load_json(pj(checkpoint_dir_model, 'eval', 'metric.json'))
        if self.eval_config['data_split'] in metric:
            tmp_metric = metric[self.eval_config['data_split']]
        else:
            tmp_model = TransformersNER(checkpoint_dir_model, max_length=self.eval_config['max_length_eval'],
                                        use_auth_token=use_auth_token)
            cache_file_feature = f"{cache_prefix}.{tmp_model.crf_layer is not None}.{self.eval_config['data_split']}.pkl"
            cache_file_prediction = pj(checkpoint_dir_model, "eval", f"prediction.{self.eval_config['data_split']}.json")
            tmp_metric = tmp_model.evaluate(batch_size=self.batch_size_eval,
                                            data_split=self.eval_config['data_split'],
                                            cache_file_feature=cache_file_feature,
                                            cache_file_prediction=cache_file_prediction)
            metric[self.eval_config['data_split']] = tmp_metric
        return metric, tmp_metric