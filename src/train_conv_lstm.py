import argparse
import datetime
import models
import os
import shutil
import time
import torch
import torch.backends.cudnn as cudnn
from config import cfg
from data import BatchDataset
from metrics import Metric
from utils import save, load, to_device, process_control, process_dataset, make_optimizer, make_scheduler, resume
from logger import Logger

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
cudnn.benchmark = True
parser = argparse.ArgumentParser(description='cfg')
for k in cfg:
    exec('parser.add_argument(\'--{0}\', default=cfg[\'{0}\'], type=type(cfg[\'{0}\']))'.format(k))
parser.add_argument('--control_name', default=None, type=str)
args = vars(parser.parse_args())
for k in cfg:
    cfg[k] = args[k]
if args['control_name']:
    cfg['control'] = {k: v for k, v in zip(cfg['control'].keys(), args['control_name'].split('_'))} \
        if args['control_name'] != 'None' else {}
cfg['control_name'] = '_'.join([cfg['control'][k] for k in cfg['control']]) if 'control' in cfg else ''
cfg['pivot_metric'] = 'Loss'
cfg['pivot'] = float('inf')
cfg['metric_name'] = {'train': ['Loss'], 'test': ['Loss']}
cfg['ae_name'] = 'vqvae'
cfg['model_name'] = 'conv_lstm'
cfg['shuffle'] = {'train': False, 'test': False}

def main():
    process_control()
    seeds = list(range(cfg['init_seed'], cfg['init_seed'] + cfg['num_experiments']))
    for i in range(cfg['num_experiments']):
        ae_tag_list = [str(seeds[i]), cfg['data_name'], cfg['subset'], cfg['ae_name'], 'code'+str(128//2**cfg['vqvae']['depth']), cfg['control_name']]
        model_tag_list = [str(seeds[i]), cfg['data_name'], cfg['subset'], cfg['model_name'], *ae_tag_list[3:5], cfg['control_name']]
        cfg['ae_tag'] = '_'.join([x for x in ae_tag_list if x])
        cfg['model_tag'] = '_'.join([x for x in model_tag_list if x])
        print('Experiment: {}'.format(cfg['model_tag']))
        runExperiment()
    return


def runExperiment():
    seed = int(cfg['model_tag'].split('_')[0])
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    dataset = {'train' : {}, 'test' : {}}
    dataset['train']['code'] = load('./output/code/train_{}.pt'.format(cfg['ae_tag']))
    dataset['train']['quantized'] = load('./output/quantized/train_{}.pt'.format(cfg['ae_tag']))
    dataset['test']['code'] = load('./output/code/test_{}.pt'.format(cfg['ae_tag']))
    dataset['test']['quantized'] = load('./output/quantized/test_{}.pt'.format(cfg['ae_tag']))
    process_dataset(dataset)
    model = eval('models.{}().to(cfg["device"])'.format(cfg['model_name']))
    optimizer = make_optimizer(model)
    scheduler = make_scheduler(optimizer)
    if cfg['resume_mode'] == 1:
        last_epoch, model, optimizer, scheduler, logger = resume(model, cfg['model_tag'], optimizer, scheduler)
    elif cfg['resume_mode'] == 2:
        last_epoch = 1
        _, model, _, _, _ = resume(model, cfg['model_tag'])
        current_time = datetime.datetime.now().strftime('%b%d_%H-%M-%S')
        logger_path = 'output/runs/{}_{}'.format(cfg['model_tag'], current_time)
        logger = Logger(logger_path)
    else:
        last_epoch = 1
        current_time = datetime.datetime.now().strftime('%b%d_%H-%M-%S')
        logger_path = 'output/runs/train_{}_{}'.format(cfg['model_tag'], current_time)
        logger = Logger(logger_path)
    if cfg['world_size'] > 1:
        model = torch.nn.DataParallel(model, device_ids=list(range(cfg['world_size'])))
    for epoch in range(last_epoch, cfg['num_epochs'] + 1):
        logger.safe(True)
        train(dataset['train'], model, optimizer, logger, epoch)
        test(dataset['test'], model, logger, epoch)
        if cfg['scheduler_name'] == 'ReduceLROnPlateau':
            scheduler.step(metrics=logger.mean['train/{}'.format(cfg['pivot_metric'])])
        else:
            scheduler.step()
        logger.safe(False)
        model_state_dict = model.module.state_dict() if cfg['world_size'] > 1 else model.state_dict()
        save_result = {
            'cfg': cfg, 'epoch': epoch + 1, 'model_dict': model_state_dict,
            'optimizer_dict': optimizer.state_dict(), 'scheduler_dict': scheduler.state_dict(),
            'logger': logger}
        save(save_result, './output/model/{}_checkpoint.pt'.format(cfg['model_tag']))
        if cfg['pivot'] > logger.mean['test/{}'.format(cfg['pivot_metric'])]:
            cfg['pivot'] = logger.mean['test/{}'.format(cfg['pivot_metric'])]
            shutil.copy('./output/model/{}_checkpoint.pt'.format(cfg['model_tag']),
                        './output/model/{}_best.pt'.format(cfg['model_tag']))
        logger.reset()
    logger.safe(False)
    return


def train(dataset, model, optimizer, logger, epoch):
    metric = Metric()
    model.train(True)
    start_time = time.time()
    dataset = BatchDataset(dataset, cfg['bptt'])
    
    hidden = [[None for _ in range(cfg['conv_lstm']['num_layers'])], [None for _ in range(cfg['conv_lstm']['num_layers'])]] # two hidden
    for k in range(cfg['conv_lstm']['num_layers']):
        new_hidden = init_hidden((dataset[0]['code'].size(0), cfg['conv_lstm']['output_size'], *dataset[0]['code'].size()[2:]))
        hidden[0][k], hidden[1][k] = new_hidden[0][0], new_hidden[1][0]
    
    for i, input in enumerate(dataset):
        input_size = input['code'].size(0)
        input = to_device(input, cfg['device'])
        optimizer.zero_grad()
        for k in range(cfg['conv_lstm']['num_layers']):
            hidden[0][k], hidden[1][k] = repackage_hidden(hidden[0][k]), repackage_hidden(hidden[1][k])
        output, hidden = model(input, hidden)
        output['loss'] = output['loss'].mean() if cfg['world_size'] > 1 else output['loss']
        output['loss'].backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
        optimizer.step()
        evaluation = metric.evaluate(cfg['metric_name']['train'], input, output)
        logger.append(evaluation, 'train', n=input_size)
        if i % int((len(dataset) * cfg['log_interval']) + 1) == 0:
            batch_time = (time.time() - start_time) / (i + 1)
            lr = optimizer.param_groups[0]['lr']
            epoch_finished_time = datetime.timedelta(seconds=round(batch_time * (len(dataset) - i - 1)))
            exp_finished_time = epoch_finished_time + datetime.timedelta(
                seconds=round((cfg['num_epochs'] - epoch) * batch_time * len(dataset)))
            info = {'info': ['Model: {}'.format(cfg['model_tag']),
                             'Train Epoch: {}({:.0f}%)'.format(epoch, 100. * i / len(dataset)),
                             'Learning rate: {}'.format(lr), 'Epoch Finished Time: {}'.format(epoch_finished_time),
                             'Experiment Finished Time: {}'.format(exp_finished_time)]}
            logger.append(info, 'train', mean=False)
            logger.write('train', cfg['metric_name']['train'])
    return


def test(dataset, model, logger, epoch):
    with torch.no_grad():
        metric = Metric()
        model.train(False)
        dataset = BatchDataset(dataset, cfg['bptt'])
        for i, input in enumerate(dataset):
            input_size = input['code'].size(0)
            input = to_device(input, cfg['device'])
            output = model(input)
            output['loss'] = output['loss'].mean() if cfg['world_size'] > 1 else output['loss']
            evaluation = metric.evaluate(cfg['metric_name']['test'], input, output)
            logger.append(evaluation, 'test', input_size)
        info = {'info': ['Model: {}'.format(cfg['model_tag']), 'Test Epoch: {}({:.0f}%)'.format(epoch, 100.)]}
        logger.append(info, 'test', mean=False)
        logger.write('test', cfg['metric_name']['test'])
    return


def init_hidden(hidden_size, dtype=torch.float):
    hidden = [[torch.zeros(hidden_size, device=cfg['device'], dtype=dtype)],
              [torch.zeros(hidden_size, device=cfg['device'], dtype=dtype)]]
    return hidden


def repackage_hidden(h):
    """Wraps hidden states in new Tensors, to detach them from their history."""
    return h.detach()


if __name__ == "__main__":
    main()