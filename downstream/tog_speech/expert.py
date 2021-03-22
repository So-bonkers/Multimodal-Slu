"""Downstream expert for Spoken Term Detection on Speech Commands."""

import re
import os
import hashlib
from pathlib import Path
from typing import List, Tuple, Union

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, WeightedRandomSampler
from torch.nn.utils.rnn import pad_sequence

from .model import Model
from .dataset import SpeechCommandsDataset, SpeechCommandsTestingDataset

import pandas as pd
from sklearn.model_selection import train_test_split

class DownstreamExpert(nn.Module):
    """
    Used to handle downstream-specific operations
    eg. downstream forward, metric computation, contents to log
    """

    def __init__(self, upstream_dim: int, downstream_expert: dict, expdir: str, **kwargs):
        super(DownstreamExpert, self).__init__()
        self.upstream_dim = upstream_dim
        self.datarc = downstream_expert["datarc"]
        self.modelrc = downstream_expert["modelrc"]
        # load csv here, do stratified split
        df = pd.read_csv('/home/ml/pushkal/Multimodal-Slu/scripts/435.csv')
        classes = df.label.unique().tolist()
        print(classes)
        import downstream.tog_speech.dataset as ds
        setattr(ds, 'CLASSES', classes)
        # add logic here to remove classes
        train_df, test_df = train_test_split(df, test_size=0.15, random_state=42, stratify=df['label'])
        train_list = train_df.values.tolist()
        valid_list = test_df.values.tolist()
        self.train_dataset = SpeechCommandsDataset(train_list, classes, **self.datarc)
        self.dev_dataset = SpeechCommandsDataset(valid_list, classes, **self.datarc)
        #self.test_dataset = SpeechCommandsTestingDataset(**self.datarc)

        self.model = Model(
            input_dim=upstream_dim,
            output_class_num=len(classes),
            **self.modelrc,
        )
        self.objective = nn.CrossEntropyLoss()

        self.logging = os.path.join(expdir, 'log.log')

    def _get_balanced_dataloader(self, dataset, drop_last=False):
        return DataLoader(
            dataset,
            sampler=WeightedRandomSampler(
                dataset.sample_weights, len(dataset.sample_weights)
            ),
            batch_size=self.datarc["batch_size"],
            drop_last=drop_last,
            num_workers=self.datarc["num_workers"],
            collate_fn=dataset.collate_fn,
        )

    def _get_dataloader(self, dataset):
        return DataLoader(
            dataset,
            shuffle=False,
            batch_size=self.datarc["batch_size"],
            drop_last=False,
            num_workers=self.datarc["num_workers"],
            collate_fn=dataset.collate_fn,
        )

    # Interface
    def get_train_dataloader(self):
        return self._get_balanced_dataloader(self.train_dataset, drop_last=True)

    # Interface
    def get_dev_dataloader(self):
        return self._get_balanced_dataloader(self.dev_dataset, drop_last=False)

    # Interface
    def get_test_dataloader(self):
        return self._get_dataloader(self.test_dataset)

    # Interface
    def forward(self, features, labels, records, logger, prefix, global_step, **kwargs):
        features = pad_sequence(features, batch_first=True)
        predicted = self.model(features)

        labels = torch.LongTensor(labels).to(features.device)
        loss = self.objective(predicted, labels)

        predicted_classid = predicted.max(dim=-1).indices
        records["acc"] += (predicted_classid == labels).view(-1).cpu().float().tolist()

        return loss, predicted_classid.cpu().float().tolist(), labels.cpu().float().tolist()

    # interface
    def log_records(self, records, logger, prefix, global_step, **kwargs):
        for key, values in records.items():
            average = torch.FloatTensor(values).mean().item()
            logger.add_scalar(f"{prefix}{key}", average, global_step=global_step)
            with open(self.logging, 'a') as f:
                f.write(f'{prefix}|step:{global_step}|{key}:{average}\n')


def split_dataset(
    root_dir: Union[str, Path], max_uttr_per_class=2 ** 27 - 1
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """Split Speech Commands into 3 set.
    
    Args:
        root_dir: speech commands dataset root dir
        max_uttr_per_class: predefined value in the original paper
    
    Return:
        train_list: [(class_name, audio_path), ...]
        valid_list: as above
    """
    train_list, valid_list = [], []

    for entry in Path(root_dir).iterdir():
        if not entry.is_dir() or entry.name == "_background_noise_":
            continue

        for audio_path in entry.glob("*.wav"):
            speaker_hashed = re.sub(r"_nohash_.*$", "", audio_path.name)
            hashed_again = hashlib.sha1(speaker_hashed.encode("utf-8")).hexdigest()
            percentage_hash = (int(hashed_again, 16) % (max_uttr_per_class + 1)) * (
                100.0 / max_uttr_per_class
            )

            if percentage_hash < 10:
                valid_list.append((entry.name, audio_path))
            elif percentage_hash < 20:
                pass  # testing set is discarded
            else:
                train_list.append((entry.name, audio_path))

    return train_list, valid_list
