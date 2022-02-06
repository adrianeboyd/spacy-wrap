"""
TODO
- [ ] check serialization
- [ ] setup training + loss
  - [ ] test training
"""


from typing import Callable, List, Iterable, Dict, Optional, Iterator
import warnings

from spacy.language import Language
from spacy.tokens import Doc
from spacy.vocab import Vocab

from spacy_transformers.data_classes import FullTransformerBatch
from spacy.pipeline.trainable_pipe import TrainablePipe
from spacy_transformers.annotation_setters import null_annotation_setter
from thinc.api import Config, Model

from .util import split_by_doc, softmax


DEFAULT_CONFIG_STR = """
[classification_transformer]
max_batch_items = 4096
doc_extension_trf_data = "clf_trf_data"
doc_extension_prediction = "classification"
labels = ["positive", negative"]

[classification_transformer.set_extra_annotations]
@annotation_setters = "spacy-transformers.null_annotation_setter.v1"

[classification_transformer.model]
@architectures = "spacy-wrap.ClassificationTransformerModel.v1"
name = "roberta-base"
tokenizer_config = {"use_fast": true}
transformer_config = {}
mixed_precision = false
grad_scaler_config = {}

[classification_transformer.model.get_spans]
@span_getters = "spacy-transformers.strided_spans.v1"
window = 128
stride = 96
"""

DEFAULT_CONFIG = Config().from_str(DEFAULT_CONFIG_STR)


@Language.factory(
    "classification_transformer",
    default_config=DEFAULT_CONFIG["classification_transformer"],
)
def make_classification_transformer(
    nlp: Language,
    name: str,
    model: Model[List[Doc], FullTransformerBatch],
    set_extra_annotations: Callable[[List[Doc], FullTransformerBatch], None],
    max_batch_items: int,
    doc_extension_trf_data: str,
    doc_extension_prediction: str,
    labels: List[str],
):
    """Construct a ClassificationTransformer component, which lets you plug a model from the
    Huggingface transformers library into spaCy so you can use it in your
    pipeline. One or more subsequent spaCy components can use the transformer
    outputs as features in its model, with gradients backpropagated to the single
    shared weights.

    Args:
        model (Model[List[Doc], FullTransformerBatch]): A thinc Model object wrapping
            the transformer. Usually you will want to use the ClassificationTransformer
            layer for this.
        set_extra_annotations (Callable[[List[Doc], FullTransformerBatch], None]): A
            callback to set additional information onto the batch of `Doc` objects.
            The doc._.{doc_extension_trf_data} attribute is set prior to calling the callback
            as well as doc._.{doc_extension_prediction} and doc._.{doc_extension_prediction}_prob.
            By default, no additional annotations are set.
        labels (List[str]): A list of labels which the transformer model outputs, should be ordered.
    """
    clf_trf = ClassificationTransformer(
        nlp.vocab,
        model,
        set_extra_annotations,
        max_batch_items=max_batch_items,
        name=name,
        labels=labels,
        doc_extension_trf_data=doc_extension_trf_data,
        doc_extension_prediction=doc_extension_prediction,
    )
    clf_trf.model.initialize()
    return clf_trf


class ClassificationTransformer(TrainablePipe):
    """spaCy pipeline component that provides access to a transformer model from
    the Huggingface transformers library. Usually you will connect subsequent
    components to the shared transformer using the TransformerListener layer.
    This works similarly to spaCy's Tok2Vec component and Tok2VecListener
    sublayer.

    The activations from the transformer are saved in the doc._.trf_data extension
    attribute. You can also provide a callback to set additional annotations.

    Args:
        vocab (Vocab): The Vocab object for the pipeline.
        model (Model[List[Doc], FullTransformerBatch]): A thinc Model object wrapping
            the transformer. Usually you will want to use the TransformerModel
            layer for this.
        set_extra_annotations (Callable[[List[Doc], FullTransformerBatch], None]): A
            callback to set additional information onto the batch of `Doc` objects.
            The doc._.{doc_extension_trf_data} attribute is set prior to calling the callback
            as well as doc._.{doc_extension_prediction} and doc._.{doc_extension_prediction}_prob.
            By default, no additional annotations are set.
        labels (List[str]): A list of labels which the transformer model outputs, should be ordered.
    """

    def __init__(
        self,
        vocab: Vocab,
        model: Model[List[Doc], FullTransformerBatch],
        labels: List[str],
        doc_extension_trf_data: str,
        doc_extension_prediction: str,
        set_extra_annotations: Callable = null_annotation_setter,
        *,
        name: str = "classification_transformer",
        max_batch_items: int = 128 * 32,  # Max size of padded batch
    ):
        """Initialize the transformer component."""
        self.name = name
        self.vocab = vocab
        self.model = model
        if not isinstance(self.model, Model):
            raise ValueError(f"Expected Thinc Model, got: {type(self.model)}")
        self.set_extra_annotations = set_extra_annotations
        self.cfg = {"max_batch_items": max_batch_items}
        self.doc_extension_trf_data = doc_extension_trf_data

        install_extensions(self.doc_extension_trf_data)

        install_classification_extensions(
            doc_extension_prediction, labels, doc_extension_trf_data
        )

    def set_annotations(
        self, docs: Iterable[Doc], predictions: FullTransformerBatch
    ) -> None:
        """Assign the extracted features to the Doc objects. By default, the
        TransformerData object is written to the doc._.{doc_extension_trf_data} attribute. Your
        set_extra_annotations callback is then called, if provided.

        Args:
            docs (Iterable[Doc]): The documents to modify.
            predictions: (FullTransformerBatch): A batch of activations.
        """
        doc_data = split_by_doc(predictions)
        for doc, data in zip(docs, doc_data):
            setattr(doc._, self.doc_extension_trf_data, data)
        self.set_extra_annotations(docs, predictions)

    def __call__(self, doc: Doc) -> Doc:
        """Apply the pipe to one document. The document is modified in place,
        and returned. This usually happens under the hood when the nlp object
        is called on a text and all components are applied to the Doc.
        docs (Doc): The Doc to process.
        RETURNS (Doc): The processed Doc.
        DOCS: https://spacy.io/api/transformer#call
        """
        install_extensions(self.doc_extension_trf_data)
        outputs = self.predict([doc])
        self.set_annotations([doc], outputs)
        return doc

    def pipe(self, stream: Iterable[Doc], *, batch_size: int = 128) -> Iterator[Doc]:
        """Apply the pipe to a stream of documents. This usually happens under
        the hood when the nlp object is called on a text and all components are
        applied to the Doc.
        stream (Iterable[Doc]): A stream of documents.
        batch_size (int): The number of documents to buffer.
        YIELDS (Doc): Processed documents in order.
        DOCS: https://spacy.io/api/transformer#pipe
        """
        install_extensions(self.doc_extension_trf_data)
        for outer_batch in minibatch(stream, batch_size):
            outer_batch = list(outer_batch)
            for indices in batch_by_length(outer_batch, self.cfg["max_batch_items"]):
                subbatch = [outer_batch[i] for i in indices]
                self.set_annotations(subbatch, self.predict(subbatch))
            yield from outer_batch

    def predict(self, docs: Iterable[Doc]) -> FullTransformerBatch:
        """Apply the pipeline's model to a batch of docs, without modifying them.
        Returns the extracted features as the FullTransformerBatch dataclass.
        docs (Iterable[Doc]): The documents to predict.
        RETURNS (FullTransformerBatch): The extracted features.
        DOCS: https://spacy.io/api/transformer#predict
        """
        if not any(len(doc) for doc in docs):
            # Handle cases where there are no tokens in any docs.
            activations = FullTransformerBatch.empty(len(docs))
        else:
            activations = self.model.predict(docs)
        return activations

    def update(
        self,
        examples: Iterable[Example],
        *,
        drop: float = 0.0,
        sgd: Optional[Optimizer] = None,
        losses: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        pass

    def get_loss(self, docs, golds, scores):
        pass

    def initialize(
        self,
        get_examples: Callable[[], Iterable[Example]],
        *,
        nlp: Optional[Language] = None,
    ):
        """Initialize the pipe for training, using data examples if available.
        get_examples (Callable[[], Iterable[Example]]): Optional function that
            returns gold-standard Example objects.
        nlp (Language): The current nlp object.
        DOCS: https://spacy.io/api/transformer#initialize
        """
        validate_get_examples(get_examples, "Transformer.initialize")
        self.model.initialize()

    def to_disk(
        self, path: Union[str, Path], *, exclude: Iterable[str] = tuple()
    ) -> None:
        """Serialize the pipe to disk.
        path (str / Path): Path to a directory.
        exclude (Iterable[str]): String names of serialization fields to exclude.
        DOCS: https://spacy.io/api/transformer#to_disk
        """
        serialize = {}
        serialize["cfg"] = lambda p: srsly.write_json(p, self.cfg)
        serialize["vocab"] = lambda p: self.vocab.to_disk(p)
        serialize["model"] = lambda p: self.model.to_disk(p)
        util.to_disk(path, serialize, exclude)

    def from_disk(
        self, path: Union[str, Path], *, exclude: Iterable[str] = tuple()
    ) -> "Transformer":
        """Load the pipe from disk.
        path (str / Path): Path to a directory.
        exclude (Iterable[str]): String names of serialization fields to exclude.
        RETURNS (Transformer): The loaded object.
        DOCS: https://spacy.io/api/transformer#from_disk
        """

        def load_model(p):
            try:
                with open(p, "rb") as mfile:
                    self.model.from_bytes(mfile.read())
            except AttributeError:
                raise ValueError(Errors.E149) from None
            except (IsADirectoryError, PermissionError):
                warn_msg = (
                    "Automatically converting a transformer component "
                    "from spacy-transformers v1.0 to v1.1+. If you see errors "
                    "or degraded performance, download a newer compatible "
                    "model or retrain your custom model with the current "
                    "spacy-transformers version. For more details and "
                    "available updates, run: python -m spacy validate"
                )
                warnings.warn(warn_msg)
                p = Path(p).absolute()
                hf_model = huggingface_from_pretrained(
                    p,
                    self.model._init_tokenizer_config,
                    self.model._init_transformer_config,
                )
                self.model.attrs["set_transformer"](self.model, hf_model)

        deserialize = {
            "vocab": self.vocab.from_disk,
            "cfg": lambda p: self.cfg.update(deserialize_config(p)),
            "model": load_model,
        }
        util.from_disk(path, deserialize, exclude)
        return self


def install_extensions(doc_ext_attr) -> None:
    if not Doc.has_extension(doc_ext_attr):
        Doc.set_extension(doc_ext_attr, default=None)


def install_classification_extensions(
    doc_extension_prediction: str,
    labels: list,
    doc_extension: str,
):
    prob_getter, label_getter = make_classification_getter(
        doc_extension_prediction, labels, doc_extension
    )
    if not Doc.has_extension(f"{doc_extension_prediction}_prob"):
        Doc.set_extension(f"{doc_extension_prediction}_prob", getter=prob_getter)
    if not Doc.has_extension(doc_extension_prediction):
        Doc.set_extension(doc_extension_prediction, getter=label_getter)


def make_classification_getter(
    doc_extension_prediction, labels, doc_extension_trf_data
):
    def prob_getter(doc) -> dict:
        trf_data = getattr(doc._, doc_extension_trf_data)
        if trf_data.tensors:
            return {
                "prob": softmax(trf_data.tensors[0][0]).round(decimals=3),
                "labels": labels,
            }
        else:
            warnings.warn(
                "The tensors from the transformer forward pass is empty this is likely caused by an empty input string. Thus the model will return None"
            )
            return {
                "prob": None,
                "labels": labels,
            }

    def label_getter(doc) -> Optional[str]:
        prob = getattr(doc._, f"{doc_extension_prediction}_prob")
        if prob["prob"] is not None:
            return labels[int(prob["prob"].argmax())]
        else:
            return None

    return prob_getter, label_getter
