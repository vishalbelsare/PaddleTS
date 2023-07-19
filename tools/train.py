import os
import numpy as np
import random
import argparse
import warnings

import paddle
from paddlets.utils.config import Config
from paddlets.datasets.repository import get_dataset
from paddlets.transform.sklearn_transforms import StandardScaler
from paddlets.utils.manager import MODELS
from paddlets.metrics import MSE, MAE
from paddlets.utils import backtest
from paddlets.logger import Logger

logger = Logger(__name__)
warnings.filterwarnings("ignore")


def parse_args():
    parser = argparse.ArgumentParser(description='Time Series Forecasting')
    # Common params
    parser.add_argument("--config", help="The path of config file.", type=str)
    parser.add_argument(
        '--device',
        help='Set the device place for training model.',
        default='gpu',
        choices=['cpu', 'gpu'],
        type=str)
    parser.add_argument(
        '--save_dir',
        help='The directory for saving the model snapshot.',
        type=str,
        default='./output/')
    parser.add_argument(
        '--checkpoints',
        help='model checkpoints for eval.',
        type=str,
        default=None)
    # Runntime params
    parser.add_argument(
        '--seq_len', help='input length in training.', type=int)
    parser.add_argument(
        '--predict_len', help='output length in training.', type=int)
    parser.add_argument('--epoch', help='Iterations in training.', type=int)
    parser.add_argument(
        '--batch_size', help='Mini batch size of one gpu or cpu. ', type=int)
    parser.add_argument('--learning_rate', help='Learning rate.', type=float)

    # Other params
    parser.add_argument(
        '--seed',
        help='Set the random seed in training.',
        default=42,
        type=int)
    parser.add_argument(
        '--opts', help='Update the key-value pairs of all options.', nargs='+')

    return parser.parse_args()


def main(args):

    paddle.set_device(args.device)
    seed = args.seed
    paddle.seed(seed)
    np.random.seed(seed)
    random.seed(seed)

    assert args.config is not None, \
        'No configuration file specified, please set --config'

    cfg = Config(
        args.config,
        learning_rate=args.learning_rate,
        epoch=args.epoch,
        seq_len=args.seq_len,
        predict_len=args.predict_len,
        batch_size=args.batch_size,
        opts=args.opts)

    batch_size = cfg.batch_size
    dataset = cfg.dataset
    predict_len = cfg.predict_len
    seq_len = cfg.seq_len
    epoch = cfg.epoch
    split = dataset.get('split', None)
    do_eval = cfg.dic.get('do_eval', True)
    logger.info(cfg.__dict__)

    if not os.path.exists(args.save_dir):
        os.makedirs(args.save_dir)
    else:
        import shutil
        shutil.rmtree(args.save_dir)
        os.makedirs(args.save_dir)

    ts_val = None
    if dataset['name'] == 'Dataset':
        import pandas as pd
        from paddlets import TSDataset
        dataset_root = dataset.get('dataset_root', None)

        if not os.path.exists(dataset_root):
            raise FileNotFoundError('there is not `dataset_root`: {}.'.format(
                dataset_root))
        df = pd.read_csv(dataset['train_path'])

        if cfg.dic.get('info_params', None) is None:
            raise ValueError("`info_params` is necessary, but it is None.")
        else:
            info_params = cfg.dic['info_params']
            if info_params.get('time_col', None) is None:
                raise ValueError("`time_col` is necessary, but it is None.")

        ts_train = TSDataset.load_from_dataframe(df, **info_params)
        if dataset.get('val_path', False):
            if os.path.exists(dataset['val_path']):
                df = pd.read_csv(dataset['val_path'])
                ts_val = TSDataset.load_from_dataframe(df, **info_params)
    else:
        if split:
            ts_train, ts_val, ts_test = get_dataset(dataset['name'], split,
                                                    seq_len)
        else:
            ts_train = get_dataset(dataset['name'], split, seq_len)

    if cfg.model['name'] == 'PPTimes':
        from paddlets.ensemble import WeightingEnsembleForecaster
        estimators = []
        for model_name, model_cfg in cfg.model['model_cfg'].items():
            model_cfg = Config(
                model_cfg,
                seq_len=seq_len,
                predict_len=predict_len,
                batch_size=batch_size,
                opts=args.opts)
            logger.info(model_cfg.model)
            one_model = MODELS.components_dict[model_name]
            params = model_cfg.model['model_cfg']
            params['in_chunk_len'] = seq_len
            params['out_chunk_len'] = predict_len
            params['batch_size'] = batch_size
            params['max_epochs'] = epoch

            estimators.append((one_model, params))

        model = WeightingEnsembleForecaster(
            in_chunk_len=seq_len,
            out_chunk_len=predict_len,
            skip_chunk_len=0,
            estimators=estimators,
            mode='mean')

    else:
        model = MODELS.components_dict[cfg.model['name']](
            in_chunk_len=seq_len,
            out_chunk_len=predict_len,
            batch_size=batch_size,
            max_epochs=epoch,
            **cfg.model['model_cfg'])

    if dataset.get('scale', False):
        logger.info('start scaling...')
        scaler = StandardScaler()
        scaler.fit(ts_train)
        ts_train = scaler.transform(ts_train)
        if ts_val is not None:
            ts_val = scaler.transform(ts_val)
        import joblib
        joblib.dump(scaler, os.path.join(args.save_dir, 'scaler.pkl'))

    if cfg.dataset.get('time_feat', 'False'):
        logger.info('generate times feature')
        from paddlets.transform import TimeFeatureGenerator
        if dataset.get('use_holiday', False):
            time_feature_generator = TimeFeatureGenerator(feature_cols=[
                'minuteofhour', 'hourofday', 'dayofmonth', 'dayofweek',
                'dayofyear', 'monthofyear', 'weekofyear', 'holidays'
            ])
            if dataset['name'] != 'Dataset':
                ts_all = get_dataset(dataset['name'])
                ts_all = time_feature_generator.fit_transform(ts_all)
                ts_train._known_cov = ts_all._known_cov[split['train'][0]:
                                                        split['train'][1]]
                ts_val._known_cov = ts_all._known_cov[split['val'][0] -
                                                      seq_len:split['val'][1]]
            else:
                ts_train = time_feature_generator.fit_transform(ts_train)
                if ts_val is not None:
                    ts_val = time_feature_generator.fit_transform(ts_val)

        else:
            time_feature_generator = TimeFeatureGenerator(feature_cols=[
                'hourofday', 'dayofmonth', 'dayofweek', 'dayofyear'
            ])
            ts_train = time_feature_generator.fit_transform(ts_train)
            ts_val = time_feature_generator.fit_transform(ts_val)

    logger.info('start training...')
    model.fit(ts_train, ts_val)

    logger.info('save best model...')

    if cfg.model['name'] == 'PPTimes':
        model.save(args.save_dir + '/')
    else:
        model.save(args.save_dir + '/checkpoints/')

    logger.info('done.')


if __name__ == '__main__':
    args = parse_args()
    main(args)