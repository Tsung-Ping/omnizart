import os
from os.path import join as jpath
from datetime import datetime

import h5py
import numpy as np
import tensorflow as tf

from omnizart.base import BaseTranscription, BaseDatasetLoader
from omnizart.setting_loaders import ChordSettings_2
from omnizart.io import write_yaml
from omnizart.utils import get_logger, ensure_path_exists, parallel_generator
from omnizart.constants.datasets import BeatlesStructure
from omnizart.feature.cqt import extract_cqt
from omnizart.models.t2t import MultiHeadAttention
from omnizart.chord.features_2 import extract_feature_label
from omnizart.chord.inference_2 import inference, write_csv
from omnizart.train import get_train_val_feat_file_list
from omnizart.models.chord_model import ChordModel_2, ReduceSlope, WarmupSchedule


logger = get_logger("Chord Application")


class ChordTranscription(BaseTranscription):
    """Application class for chord transcription."""
    def __init__(self, conf_path=None):
        super().__init__(ChordSettings_2, conf_path=conf_path)
        self.custom_objects = {"MultiHeadAttention": MultiHeadAttention}

    def transcribe(self, input_audio, model_path=None, output="./"):
        """Transcribe chords in the audio.

        This function transcribes chord progression in the audio and will outputs MIDI
        and CSV files. The MIDI file is provided for quick validation by directly listen
        to the chords. The complete transcription results are listed in the CSV file,
        which contains the chord's name and the start and end time.

        Parameters
        ----------
        input_audio: Path
            Path to the raw audio file (.wav).
        model_path: Path
            Path to the trained model or the supported transcription mode.
        output: Path (optional)
            Path for writing out the transcribed MIDI file. Default to the current path.

        Returns
        -------
        midi: pretty_midi.PrettyMIDI
            Transcribed chord progression with default chord-to-notes mappings.

        See Also
        --------
        omnizart.cli.chord.transcribe: CLI entry point of this function.
        omnizart.chord.inference: Records the default chord-to-notes mappings.
        """

        logger.info("Extracting feature")
        sampling_rate = 22050
        a_hop = 1024
        cqt = extract_cqt(input_audio,
                          sampling_rate=sampling_rate,
                          lowest_note=24,
                          note_num=72,
                          a_hop=a_hop,
                          pad_sec=0)
        t_unit = a_hop / sampling_rate

        logger.info("Loading model")
        model, settings = self._load_model(model_path, custom_objects=self.custom_objects)

        logger.info("Preparing feature for model prediction")

        num_steps = settings.feature.num_steps
        pad_end = num_steps - cqt.shape[0] % num_steps
        cqt_pad = np.pad(cqt, ((0, pad_end), (0, 0)), constant_values=0)

        num_seqs = cqt_pad.shape[0] // num_steps
        cqt_pad = cqt_pad.reshape([num_seqs, num_steps, cqt_pad.shape[1]])

        logger.info("Predicting...")
        chord, _, _, _ = model.predict(cqt_pad)
        chord = chord.reshape(np.prod(chord.shape))[:-pad_end]  # Reshape and remove padding

        logger.info("Infering chords...")
        midi, info = inference(chord, t_unit, min_dura=settings.inference.min_dura)

        output = self._output_midi(output=output, input_audio=input_audio, midi=midi)
        if output is not None:
            write_csv(info, output=output.replace(".mid", ".csv"))
            logger.info("MIDI and CSV file have been written to %s", os.path.abspath(os.path.dirname(output)))

        logger.info("Transcription finished")
        return midi

    def generate_feature(self, dataset_path, chord_settings=None, num_threads=4):
        """Extract feature of Beatles dataset.

        There are three main features that will be used in the training:

        * cqt: input feature of the NN model
        * chord: the first type of the ground-truth
        * chord transition: the second type of the ground-truth

        The last two feature will be both used for computing the training loss.
        During the feature extraction, the feature data is stored as a numpy array
        with named field, makes it works like a dict type.
        """
        settings = self._validate_and_get_settings(chord_settings)

        # Resolve feature output path
        train_feat_out_path, test_feat_out_path = self._resolve_feature_output_path(dataset_path, settings)
        logger.info("Output training feature to %s", train_feat_out_path)
        logger.info("Output testing feature to %s", test_feat_out_path)

        train_data_pair = BeatlesStructure.get_train_data_pair(dataset_path)
        test_data_pair = BeatlesStructure.get_test_data_pair(dataset_path)
        logger.info("Total number of training data: %d", len(train_data_pair))
        logger.info("Total number of testing data: %d", len(test_data_pair))

        # Start feature extraction
        logger.info("Start to extract training feature")
        _parallel_feature_extraction(train_data_pair, train_feat_out_path, num_threads=num_threads)

        logger.info("Start to extract testing feature")
        _parallel_feature_extraction(test_data_pair, test_feat_out_path, num_threads=num_threads)

        # Writing out the settings
        write_yaml(settings.to_json(), jpath(train_feat_out_path, ".success.yaml"))
        write_yaml(settings.to_json(), jpath(test_feat_out_path, ".success.yaml"))
        logger.info("All done")

    def train(self, feature_folder, model_name=None, input_model_path=None, chord_settings=None):
        """Model training.

        Train a new music model or continue to train on a pre-trained model.

        Parameters
        ----------
        feature_folder: Path
            Path to the generated feature.
        model_name: str
            The name of the trained model. If not given, will default to the
            current timestamp.
        input_model_path: Path
            Specify the path to the pre-trained model if you want to continue
            to fine-tune on the model.
        chord_settings: ChordSettings_2
            The configuration instance that holds all relative settings for
            the life-cycle of building a model.
        """
        settings = self._validate_and_get_settings(chord_settings)

        if input_model_path is not None:
            logger.info("Continue to train one model: %s", input_model_path)
            model, _ = self._load_model(input_model_path)

        split = settings.training.steps / (settings.training.steps + settings.training.val_steps)
        train_feat_files, val_feat_files = get_train_val_feat_file_list(feature_folder, split=split)

        output_types = (tf.float32, (tf.int32, tf.int32))
        output_shapes = (
            [settings.feature.num_steps, settings.feature.num_bins],
            ([settings.feature.num_steps], [settings.feature.num_steps])
        )
        train_dataset = BeatlesDatasetLoader(
            feature_files=train_feat_files,
            num_samples=settings.training.epoch * settings.training.batch_size * settings.training.steps
        ) \
            .get_dataset(settings.training.batch_size, output_types=output_types, output_shapes=output_shapes)
        val_dataset = BeatlesDatasetLoader(
            feature_files=val_feat_files,
            num_samples=settings.training.epoch * settings.training.val_batch_size * settings.training.val_steps
        ) \
            .get_dataset(settings.training.batch_size, output_types=output_types, output_shapes=output_shapes)

        if input_model_path is None:
            logger.info("Constructing new model")
            model = self.get_model(settings)

        # learninig_rate = tf.keras.optimizers.schedules.ExponentialDecay(
        #     settings.training.init_learning_rate,
        #     decay_steps=settings.training.steps,
        #     decay_rate=settings.training.learning_rate_decay,
        #     staircase=True
        # )
        learninig_rate = WarmupSchedule(settings.model.enc_input_emb_size)
        optimizer = tf.keras.optimizers.Adam(learning_rate=learninig_rate, clipvalue=1)
        model.compile(optimizer=optimizer, loss=chord_loss_func, metrics=["accuracy"])

        logger.info("Resolving model output path")
        if model_name is None:
            model_name = str(datetime.now()).replace(" ", "_")
        if not model_name.startswith(settings.model.save_prefix):
            model_name = settings.model.save_prefix + "_" + model_name
        model_save_path = jpath(settings.model.save_path, model_name)
        ensure_path_exists(model_save_path)
        write_yaml(settings.to_json(), jpath(model_save_path, "configurations.yaml"))
        logger.info("Model output to: %s", model_save_path)

        callbacks = [
            tf.keras.callbacks.EarlyStopping(patience=settings.training.early_stop, monitor="val_loss"),
            # tf.keras.callbacks.ModelCheckpoint(model_save_path, monitor="val_loss"),
            ReduceSlope()
        ]

        history = model.fit(
            train_dataset,
            validation_data=val_dataset,
            epochs=settings.training.epoch,
            steps_per_epoch=settings.training.steps,
            validation_steps=settings.training.val_steps,
            callbacks=callbacks
        )
        # # Save model
        # model.save(model_save_path)
        return history

    def get_model(self, settings):
        """Get the chord model.

        More comprehensive reasons to having this method, please refer to
        ``omnizart.base.BaseTranscription.get_model``.
        """
        return ChordModel_2(
            out_classes=26,
            num_enc_attn_blocks=settings.model.num_enc_attn_blocks,
            num_dec_attn_blocks=settings.model.num_dec_attn_blocks,
            n_steps=settings.feature.num_steps,
            enc_input_emb_size=settings.model.enc_input_emb_size,
            dec_input_emb_size=settings.model.dec_input_emb_size,
            dropout_rate=settings.model.dropout_rate,
            annealing_rate=settings.model.annealing_rate
        )


def _extract_feature_arg_wrapper(input_tup, **kwargs):
    return extract_feature_label(input_tup[0], input_tup[1], **kwargs)


def _parallel_feature_extraction(data_pair, out_path, num_threads=4):
    iters = enumerate(
        parallel_generator(
            _extract_feature_arg_wrapper,
            data_pair,
            max_workers=num_threads,
            chunk_size=num_threads
        )
    )
    for idx, (feature, feat_idx) in iters:
        f_name = data_pair[feat_idx][0].replace(".npy", "")
        # logger.info("Progress: %d/%d - %s", idx + 1, len(data_pair), f_name)
        print(f"Progress: {idx+1}/{len(data_pair)} - {f_name}", end="\r")
        out_hdf = jpath(out_path, os.path.basename(f_name) + ".hdf")
        _write_feature(feature, out_path=out_hdf)


def _write_feature(feature, out_path):
    key_list = ["cqt", "chord", "transition", "len", "frame_id"]
    with h5py.File(out_path, "w") as out_hdf:
        for key in key_list:
            out_hdf.create_dataset(key, data=feature[key], compression="gzip", compression_opts=3)


class BeatlesDatasetLoader(BaseDatasetLoader):
    """Beatles dataset loader.

    The feature column name stored in the .hdf files is slightly different from
    others, which the name is ``cqt``, not ``feature``.
    Also the returned label should be a tuple of two different ground-truth labels
    to fit the training scenario.

    Yields
    ------
    feature:
        Input feature for training the model.
    label: tuple
        gt_chord -> Ground-truth chord label.
        gt_transition -> Ground-truth chord change label.
    """
    def __init__(self, feature_folder=None, feature_files=None, num_samples=40, slice_hop=1):
        super().__init__(
            feature_folder=feature_folder,
            feature_files=feature_files,
            num_samples=num_samples,
            slice_hop=slice_hop,
            feat_col_name="cqt"
        )

    def _get_label(self, hdf_name, slice_start):
        gt_chord = self.hdf_refs[hdf_name]["chord"][slice_start:slice_start + self.slice_hop].squeeze()
        gt_transition = self.hdf_refs[hdf_name]["transition"][slice_start:slice_start + self.slice_hop].squeeze()
        return gt_chord, gt_transition


def chord_loss_func(
    chord,
    transition,
    logits,
    transition_logits,
    out_classes=26,
    lambda_loss_c=1,
    lambda_loss_ct=3
):
    exp_cc_logits = tf.nn.sigmoid_cross_entropy_with_logits(
        labels=tf.cast(transition, tf.float32), logits=transition_logits
    )
    loss_ct = lambda_loss_ct * tf.reduce_mean(input_tensor=exp_cc_logits)

    one_hot_chord = tf.one_hot(chord, depth=out_classes)  # pylint: disable=E1120
    # loss_c = lambda_loss_c * tf.compat.v1.losses.softmax_cross_entropy(
    #     onehot_labels=one_hot_chord, logits=logits, label_smoothing=0.1
    # )
    c_loss_fn = tf.keras.losses.CategoricalCrossentropy(from_logits=True, label_smoothing=0.1)
    loss_c = lambda_loss_c * c_loss_fn(y_true=one_hot_chord, y_pred=logits)

    return loss_ct + loss_c
