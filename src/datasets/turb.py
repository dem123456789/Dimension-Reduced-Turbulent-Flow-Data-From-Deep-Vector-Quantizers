import os
import torch
import h5py
import numpy as np
from torch.utils.data import Dataset
from utils import makedir_exist_ok, save, load


class TURB(Dataset):
    data_name = 'TURB'

    def __init__(self, root, split, download=False):
        self.root = os.path.expanduser(root)
        self.split = split
        if download:
            self.download()
        if not self._check_exists():
            raise RuntimeError('Dataset not found. You can use download=True to download it')
        self.A, self.H = load(os.path.join(self.processed_folder, '{}.pt'.format(split)))

    def __getitem__(self, index):
        A, H = torch.tensor(self.A[index]), torch.tensor(self.H[index])
        input = {'A': A, 'H': H}
        return input

    def __len__(self):
        return len(self.A)

    @property
    def processed_folder(self):
        return os.path.join(self.root, 'processed')

    @property
    def raw_folder(self):
        return os.path.join(self.root, 'raw')

    def _check_exists(self):
        return os.path.exists(self.processed_folder)

    def download(self):
        if self._check_exists():
            return
        makedir_exist_ok(self.raw_folder)
        train_set, test_set = make_dataset(self.raw_folder)
        save(train_set, os.path.join(self.processed_folder, 'train.pt'))
        save(test_set, os.path.join(self.processed_folder, 'test.pt'))
        return

    def __repr__(self):
        fmt_str = 'Dataset {}\n'.format(self.__class__.__name__)
        fmt_str += '    Number of data points: {}\n'.format(self.__len__())
        fmt_str += '    Split: {}\n'.format(self.split)
        fmt_str += '    Root: {}\n'.format(self.root)
        return fmt_str


def make_dataset(path):
    filenames = os.listdir(path)
    A, H = [], []
    for i in range(len(filenames)):
        filename_list = filenames[i].split('.')
        if filename_list[0] == 'PressureH':
            H_i = []
            f = h5py.File('{}/{}'.format(path, filenames[i]), 'r')
            for key in f:
                H_i.append(f[key][:])
            H_i = np.stack(H_i, axis=0)
            H.append(H_i)
        elif filename_list[0] == 'VelG_R':
            A_i = []
            f = h5py.File('{}/{}'.format(path, filenames[i]), 'r')
            for key in f:
                if key in ['dUdxG_R', 'dUdyG_R', 'dUdzG_R', 'dVdxG_R', 'dVdyG_R', 'dVdzG_R', 'dWdxG_R', 'dWdyG_R',
                           'dWdzG_R']:
                    A_i.append(f[key][:])
            A_i = np.stack(A_i, axis=0)
            A.append(A_i)
    cat_A, cat_H = [], []
    for i in range(4):
        cat_A.append(np.concatenate(A[i * 4:i * 4 + 4], axis=2))
        cat_H.append(np.concatenate(H[i * 4:i * 4 + 4], axis=2))
    A = np.concatenate(cat_A, axis=1)
    H = np.concatenate(cat_H, axis=1)
    from skimage.util.shape import view_as_blocks
    A = view_as_blocks(A, block_shape=(9, 32, 32, 32)).reshape(-1, 9, 32, 32, 32)
    H = view_as_blocks(H, block_shape=(6, 32, 32, 32)).reshape(-1, 6, 32, 32, 32)
    train_A, train_H, test_A, test_H = A[:50], H[:50], A[50:], H[50:]
    return (train_A, train_H), (test_A, test_H)