import copy
import logging
import os
from pathlib import Path

import numpy as np
import tensorflow as tf
import tensorflow_addons as tfa

from typing import Any, List, Optional, Text, Dict, Tuple, Union

import rasa.utils.io as io_utils
from rasa.shared.core.domain import Domain
from rasa.core.featurizers.tracker_featurizers import (
    TrackerFeaturizer,
    MaxHistoryTrackerFeaturizer,
    IntentMaxHistoryFeaturizer,
)
from rasa.core.featurizers.single_state_featurizer import (
    IntentTokenizerSingleStateFeaturizer,
)
from rasa.shared.nlu.interpreter import NaturalLanguageInterpreter
from rasa.core.policies.policy import Policy, SupportedData
from rasa.core.policies.ted_policy import TEDPolicy, TED
from rasa.core.constants import DEFAULT_POLICY_PRIORITY, DIALOGUE
from rasa.shared.core.trackers import DialogueStateTracker
from rasa.utils import train_utils
from rasa.utils.tensorflow import layers
from rasa.utils.tensorflow.transformer import TransformerEncoder
from rasa.utils.tensorflow.models import RasaModel
from rasa.utils.tensorflow.model_data import (
    RasaModelData,
    FeatureSignature,
    FeatureArray,
)
from rasa.utils.tensorflow.model_data_utils import convert_to_data_format

from rasa.utils.tensorflow.constants import (
    LABEL,
    DENSE_DIMENSION,
    ENCODING_DIMENSION,
    UNIDIRECTIONAL_ENCODER,
    TRANSFORMER_SIZE,
    NUM_TRANSFORMER_LAYERS,
    NUM_HEADS,
    BATCH_SIZES,
    BATCH_STRATEGY,
    EPOCHS,
    RANDOM_SEED,
    RANKING_LENGTH,
    LOSS_TYPE,
    SIMILARITY_TYPE,
    NUM_NEG,
    EVAL_NUM_EXAMPLES,
    EVAL_NUM_EPOCHS,
    NEGATIVE_MARGIN_SCALE,
    REGULARIZATION_CONSTANT,
    SCALE_LOSS,
    USE_MAX_NEG_SIM,
    MAX_NEG_SIM,
    MAX_POS_SIM,
    EMBEDDING_DIMENSION,
    DROP_RATE_DIALOGUE,
    DROP_RATE_LABEL,
    DROP_RATE,
    DROP_RATE_ATTENTION,
    WEIGHT_SPARSITY,
    KEY_RELATIVE_ATTENTION,
    VALUE_RELATIVE_ATTENTION,
    MAX_RELATIVE_POSITION,
    SOFTMAX,
    AUTO,
    BALANCED,
    TENSORBOARD_LOG_DIR,
    TENSORBOARD_LOG_LEVEL,
    CHECKPOINT_MODEL,
    FEATURIZERS,
)
from rasa.core.policies.ted_policy import LENGTH
from rasa.shared.nlu.constants import INTENT

logger = logging.getLogger(__name__)

DIALOGUE_FEATURES = f"{DIALOGUE}_features"
LABEL_FEATURES = f"{LABEL}_features"
LABEL_IDS = f"{LABEL}_ids"
LABEL_KEY = LABEL
LABEL_SUB_KEY = "ids"

SAVE_MODEL_FILE_NAME = "intent_ted_policy"


class IntentTEDPolicy(TEDPolicy):
    """Transformer Embedding Dialogue (TED) Policy is described in
    https://arxiv.org/abs/1910.00486.
    This policy has a pre-defined architecture, which comprises the
    following steps:
        - concatenate user input (user intent and entities), previous system actions,
          slots and active forms for each time step into an input vector to
          pre-transformer embedding layer;
        - feed it to transformer;
        - apply a dense layer to the output of the transformer to get embeddings of a
          dialogue for each time step;
        - apply a dense layer to create embeddings for system actions for each time
          step;
        - calculate the similarity between the dialogue embedding and embedded system
          actions. This step is based on the StarSpace
          (https://arxiv.org/abs/1709.03856) idea.
    """

    SUPPORTS_ONLINE_TRAINING = True

    # please make sure to update the docs when changing a default parameter
    defaults = {
        DENSE_DIMENSION: 20,
        ENCODING_DIMENSION: 50,
        # Number of units in transformer
        TRANSFORMER_SIZE: 128,
        # Number of transformer layers
        NUM_TRANSFORMER_LAYERS: 1,
        # Number of attention heads in transformer
        NUM_HEADS: 4,
        # If 'True' use key relative embeddings in attention
        KEY_RELATIVE_ATTENTION: False,
        # If 'True' use value relative embeddings in attention
        VALUE_RELATIVE_ATTENTION: False,
        # Max position for relative embeddings
        MAX_RELATIVE_POSITION: None,
        # Use a unidirectional or bidirectional encoder.
        UNIDIRECTIONAL_ENCODER: True,
        # ## Training parameters
        # Initial and final batch sizes:
        # Batch size will be linearly increased for each epoch.
        BATCH_SIZES: [64, 256],
        # Strategy used whenc creating batches.
        # Can be either 'sequence' or 'balanced'.
        BATCH_STRATEGY: BALANCED,
        # Number of epochs to train
        EPOCHS: 1,
        # Set random seed to any 'int' to get reproducible results
        RANDOM_SEED: None,
        # ## Parameters for embeddings
        # Dimension size of embedding vectors
        EMBEDDING_DIMENSION: 20,
        # The number of incorrect labels. The algorithm will minimize
        # their similarity to the user input during training.
        NUM_NEG: 20,
        # Type of similarity measure to use, either 'auto' or 'cosine' or 'inner'.
        SIMILARITY_TYPE: AUTO,
        # The type of the loss function, either 'softmax' or 'margin'.
        LOSS_TYPE: SOFTMAX,
        # Number of top actions to normalize scores for loss type 'softmax'.
        # Set to 0 to turn off normalization.
        RANKING_LENGTH: 10,
        # Indicates how similar the algorithm should try to make embedding vectors
        # for correct labels.
        # Should be 0.0 < ... < 1.0 for 'cosine' similarity type.
        MAX_POS_SIM: 0.8,
        # Maximum negative similarity for incorrect labels.
        # Should be -1.0 < ... < 1.0 for 'cosine' similarity type.
        MAX_NEG_SIM: -0.2,
        # If 'True' the algorithm only minimizes maximum similarity over
        # incorrect intent labels, used only if 'loss_type' is set to 'margin'.
        USE_MAX_NEG_SIM: True,
        # If 'True' scale loss inverse proportionally to the confidence
        # of the correct prediction
        SCALE_LOSS: True,
        # ## Regularization parameters
        # The scale of regularization
        REGULARIZATION_CONSTANT: 0.001,
        # The scale of how important is to minimize the maximum similarity
        # between embeddings of different labels,
        # used only if 'loss_type' is set to 'margin'.
        NEGATIVE_MARGIN_SCALE: 0.8,
        # Dropout rate for embedding layers of dialogue features.
        DROP_RATE_DIALOGUE: 0.1,
        # Dropout rate for embedding layers of utterance level features.
        DROP_RATE: 0.0,
        # Dropout rate for embedding layers of label, e.g. action, features.
        DROP_RATE_LABEL: 0.0,
        # Dropout rate for attention.
        DROP_RATE_ATTENTION: 0,
        # Sparsity of the weights in dense layers
        WEIGHT_SPARSITY: 0.8,
        # ## Evaluation parameters
        # How often calculate validation accuracy.
        # Small values may hurt performance, e.g. model accuracy.
        EVAL_NUM_EPOCHS: 20,
        # How many examples to use for hold out validation set
        # Large values may hurt performance, e.g. model accuracy.
        EVAL_NUM_EXAMPLES: 0,
        # If you want to use tensorboard to visualize training and validation metrics,
        # set this option to a valid output directory.
        TENSORBOARD_LOG_DIR: None,
        # Define when training metrics for tensorboard should be logged.
        # Either after every epoch or for every training step.
        # Valid values: 'epoch' and 'minibatch'
        TENSORBOARD_LOG_LEVEL: "epoch",
        # Perform model checkpointing
        CHECKPOINT_MODEL: False,
        # Specify what features to use as sequence and sentence features.
        # By default all features in the pipeline are used.
        FEATURIZERS: [],
    }

    @staticmethod
    def supported_data() -> SupportedData:
        return SupportedData.ML_AND_RULE_DATA

    @staticmethod
    def _standard_featurizer(max_history: Optional[int] = None) -> TrackerFeaturizer:
        return IntentMaxHistoryFeaturizer(
            IntentTokenizerSingleStateFeaturizer(), max_history=max_history
        )

    def _create_label_data(
        self, domain: Domain, interpreter: NaturalLanguageInterpreter
    ) -> Tuple[RasaModelData, List[Dict[Text, List["Features"]]]]:
        # encode all label_ids with policies' featurizer
        state_featurizer = self.featurizer.state_featurizer
        encoded_all_labels = state_featurizer.encode_all_intents(domain, interpreter)
        print("encoded all labels", encoded_all_labels)

        attribute_data, _ = convert_to_data_format(encoded_all_labels)

        print("Attribute data after conversion", attribute_data)

        label_data = RasaModelData()
        label_data.add_data(attribute_data, key_prefix=f"{LABEL_KEY}_")

        label_ids = np.arange(len(domain.intents))
        label_data.add_features(
            LABEL_KEY,
            LABEL_SUB_KEY,
            [FeatureArray(np.expand_dims(label_ids, -1), number_of_dimensions=2)],
        )

        return label_data, encoded_all_labels

    def _separate_augmented_trackers(
        self, all_trackers: List[DialogueStateTracker]
    ) -> Tuple[List[DialogueStateTracker], List[DialogueStateTracker]]:
        """Separate

        Args:
            all_trackers:

        Returns:
        """

        augmented, non_augmented = [], []
        for tracker in all_trackers:
            augmented.append(tracker) if tracker.is_augmented else non_augmented.append(
                tracker
            )

        return augmented, non_augmented

    def train(
        self,
        all_trackers: List[DialogueStateTracker],
        domain: Domain,
        interpreter: NaturalLanguageInterpreter,
        **kwargs: Any,
    ) -> None:
        """Train the policy on given training trackers."""

        # Filter non-augmented trackers from augmented ones.
        augmented_trackers, non_augmented_trackers = self._separate_augmented_trackers(
            all_trackers
        )

        print(len(non_augmented_trackers), len(augmented_trackers))

        self._label_data, encoded_all_labels = self._create_label_data(
            domain, interpreter
        )

        print("Label data signature", self._label_data.get_signature())

        model_train_data, train_label_ids = self._featurize_for_model(
            domain, encoded_all_labels, interpreter, non_augmented_trackers, **kwargs
        )

        print("model_data signature", model_train_data.get_signature())

        if model_train_data.is_empty():
            logger.error(
                f"Can not train '{self.__class__.__name__}'. No data was provided. "
                f"Skipping training of the policy."
            )
            return

        # keep one example for persisting and loading
        self.data_example = model_train_data.first_data_example()

        self.model = IntentTED(
            model_train_data.get_signature(),
            self.config,
            isinstance(self.featurizer, MaxHistoryTrackerFeaturizer),
            self._label_data,
        )

        self.model.fit(
            model_train_data,
            self.config[EPOCHS],
            self.config[BATCH_SIZES],
            self.config[EVAL_NUM_EXAMPLES],
            self.config[EVAL_NUM_EPOCHS],
            batch_strategy=self.config[BATCH_STRATEGY],
        )

        # Featurize augmented trackers for threshold computation
        model_threshold_data, threshold_label_ids = self._featurize_for_model(
            domain, encoded_all_labels, interpreter, augmented_trackers, **kwargs
        )

        self.model.compute_thresholds(model_threshold_data, threshold_label_ids)

    def _featurize_for_model(
        self,
        domain,
        encoded_all_labels,
        interpreter,
        non_augmented_trackers,
        **kwargs: Any,
    ):
        # dealing with training data
        tracker_state_features, label_ids = self.featurize_for_training(
            non_augmented_trackers, domain, interpreter, **kwargs
        )
        # extract actual training data to feed to model
        model_data = self._create_model_data(
            tracker_state_features, label_ids, encoded_all_labels
        )
        return model_data, label_ids

    @classmethod
    def load(cls, path: Text) -> "TEDPolicy":
        """Loads a policy from the storage.
        **Needs to load its featurizer**
        """

        if not os.path.exists(path):
            raise Exception(
                f"Failed to load TED policy model. Path "
                f"'{os.path.abspath(path)}' doesn't exist."
            )

        model_path = Path(path)
        tf_model_file = model_path / f"{SAVE_MODEL_FILE_NAME}.tf_model"

        featurizer = TrackerFeaturizer.load(path)

        if not (model_path / f"{SAVE_MODEL_FILE_NAME}.data_example.pkl").is_file():
            return cls(featurizer=featurizer)

        loaded_data = io_utils.json_unpickle(
            model_path / f"{SAVE_MODEL_FILE_NAME}.data_example.pkl"
        )
        label_data = io_utils.json_unpickle(
            model_path / f"{SAVE_MODEL_FILE_NAME}.label_data.pkl"
        )
        meta = io_utils.pickle_load(model_path / f"{SAVE_MODEL_FILE_NAME}.meta.pkl")
        priority = io_utils.json_unpickle(
            model_path / f"{SAVE_MODEL_FILE_NAME}.priority.pkl"
        )

        model_data_example = RasaModelData(label_key=LABEL_IDS, data=loaded_data)
        meta = train_utils.update_similarity_type(meta)

        model = IntentTED.load(
            str(tf_model_file),
            model_data_example,
            data_signature=model_data_example.get_signature(),
            config=meta,
            max_history_tracker_featurizer_used=isinstance(
                featurizer, MaxHistoryTrackerFeaturizer
            ),
            label_data=label_data,
        )

        # build the graph for prediction
        predict_data_example = RasaModelData(
            label_key=LABEL_IDS,
            data={
                feature_name: features
                for feature_name, features in model_data_example.items()
                if DIALOGUE in feature_name
            },
        )
        model.build_for_predict(predict_data_example)

        return cls(featurizer=featurizer, priority=priority, model=model, **meta)


class IntentTED(TED):
    def _prepare_layers(self) -> None:

        print("preparing layers")

        print("data layers")
        for name in self.data_signature.keys():
            print(name)
            self._prepare_sparse_dense_layer_for(name, self.data_signature)
            self._prepare_encoding_layers(name)

        print("label layers")
        for name in self.label_signature.keys():
            print(name)
            self._prepare_sparse_dense_layer_for(name, self.label_signature)
            self._prepare_encoding_layers(name)

        self._prepare_transformer_layer(
            DIALOGUE, self.config[DROP_RATE_DIALOGUE], self.config[DROP_RATE_ATTENTION]
        )

        self._prepare_embed_layers(DIALOGUE)
        self._prepare_embed_layers(LABEL)

        print("layers uptil now - ", self._tf_layers.keys())

        self._prepare_multi_label_dot_product_loss(LABEL, self.config[SCALE_LOSS])

    def _create_all_labels_embed(self) -> Tuple[tf.Tensor, tf.Tensor]:
        all_label_ids = self.tf_label_data[LABEL_KEY][LABEL_SUB_KEY][0]

        all_labels_encoded = {
            key: self._encode_features_per_attribute(self.tf_label_data, key)
            for key in self.tf_label_data.keys()
            if key != LABEL_KEY
        }

        x = all_labels_encoded.pop(f"{LABEL_KEY}_{INTENT}")

        # additional sequence axis is artifact of our RasaModelData creation
        # TODO check whether this should be solved in data creation
        x = tf.squeeze(x, axis=1)

        all_labels_embed = self._tf_layers[f"embed.{LABEL}"](x)

        return all_label_ids, all_labels_embed

    def batch_loss(
        self, batch_in: Union[Tuple[tf.Tensor], Tuple[np.ndarray]]
    ) -> tf.Tensor:
        tf_batch_data = self.batch_to_model_data_format(batch_in, self.data_signature)
        print("tf_batch_data keys", tf_batch_data.keys())

        dialogue_lengths = tf.cast(tf_batch_data[DIALOGUE][LENGTH][0], tf.int32)

        all_label_ids, all_labels_embed = self._create_all_labels_embed()

        batch_label_ids = tf_batch_data[LABEL_KEY][LABEL_SUB_KEY][
            0
        ]  # This can have multiple ids
        print("Batch label ids shape", batch_label_ids.shape)

        batch_labels_embed = self._get_labels_embed(batch_label_ids, all_labels_embed)

        print("Batch labels embed shape", batch_labels_embed.shape)

        dialogue_in = self._process_batch_data(tf_batch_data)
        dialogue_embed, dialogue_mask = self._emebed_dialogue(
            dialogue_in, dialogue_lengths
        )
        dialogue_mask = tf.squeeze(dialogue_mask, axis=-1)

        loss, acc = self._tf_layers[f"loss.{LABEL}"](
            dialogue_embed,
            batch_labels_embed,
            batch_label_ids,
            all_labels_embed,
            all_label_ids,
            dialogue_mask,
        )

        self.action_loss.update_state(loss)
        self.action_acc.update_state(acc)

        return loss

    def batch_predict(
        self, batch_in: Union[Tuple[tf.Tensor], Tuple[np.ndarray]]
    ) -> Dict[Text, tf.Tensor]:

        tf_batch_data = self.batch_to_model_data_format(
            batch_in, self.predict_data_signature
        )

        dialogue_lengths = tf.cast(tf_batch_data[DIALOGUE][LENGTH][0], tf.int32)

        if self.all_labels_embed is None:
            _, self.all_labels_embed = self._create_all_labels_embed()

        dialogue_in = self._process_batch_data(tf_batch_data)
        dialogue_embed, dialogue_mask = self._emebed_dialogue(
            dialogue_in, dialogue_lengths
        )
        dialogue_mask = tf.squeeze(dialogue_mask, axis=-1)

        sim_all = self._tf_layers[f"loss.{LABEL}"].sim(
            dialogue_embed[:, :, tf.newaxis, :],
            self.all_labels_embed[tf.newaxis, tf.newaxis, :, :],
            dialogue_mask,
        )

        scores = self._tf_layers[f"loss.{LABEL}"].confidence_from_sim(
            sim_all, self.config[SIMILARITY_TYPE]
        )

        return {"action_scores": scores, "sim_all": sim_all}

    def predict(self, predict_data: RasaModelData) -> Dict[Text, tf.Tensor]:
        if self._predict_function is None:
            logger.debug("There is no tensorflow prediction graph.")
            self.build_for_predict(predict_data)

        self._training = False  # needed for eager mode

        all_outputs = {"intent_scores": None, "similarities": None}

        # Prepare multiple batches and collect outputs
        # total_num_examples = predict_data.number_of_examples()
        # batch_size = 1
        # for index in range(0, total_num_examples, batch_size):
        #
        #     current_batch_size = min(batch_size, total_num_examples - index)
        #     batch_in = predict_data.prepare_batch(start=index, end=index+current_batch_size)
        #     batch_output = self._predict_function(batch_in)
        #     all_outputs["similarities"] = (
        #         tf.stack(all_outputs["similarities"], batch_output["similarities"])
        #         if all_outputs["similarities"]
        #         else batch_output["similarities"]
        #     )
        #     all_outputs["intent_scores"] = (
        #         tf.stack(all_outputs["intent_scores"], batch_output["intent_scores"])
        #         if all_outputs["intent_scores"]
        #         else batch_output["intent_scores"]
        #     )

        batch_in = predict_data.prepare_batch()
        batch_output = self._predict_function(batch_in)

        return batch_output

    def compute_thresholds(self, model_data: RasaModelData, label_ids):

        model_outputs = self.predict(model_data)
        print(model_outputs.keys())
