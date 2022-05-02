import itertools
import statistics
from pathlib import Path
from typing import Dict, List

import numpy as np
from disease_codification.custom_io import create_dir_if_dont_exist, load_mappings, load_pickle, save_as_pickle
from disease_codification.flair_utils import read_augmentation_corpora, read_corpus
from disease_codification.gcp import download_blob_file, upload_blob_file
from disease_codification.metrics import Metrics
from disease_codification.process_dataset.mapper import Augmentation
from disease_codification.utils import chunks, label_in_cluster
from flair.data import MultiCorpus, Sentence
from flair.embeddings import TransformerDocumentEmbeddings
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import average_precision_score, classification_report, f1_score
from sklearn.multiclass import OneVsRestClassifier
from sklearn.preprocessing import MultiLabelBinarizer
from xgboost import XGBClassifier


class Ranker:
    def __init__(
        self,
        indexers_path: Path,
        models_path: Path,
        indexer: str,
        cluster_classifier={},
        cluster_label_binarizer={},
        cluster_tfidf: Dict[str, TfidfVectorizer] = {},
    ):
        self.indexers_path: Path = indexers_path
        self.models_path: Path = models_path
        self.indexer: str = indexer
        self.cluster_classifier: Dict[str, OneVsRestClassifier] = cluster_classifier
        self.cluster_label_binarizer: Dict[str, MultiLabelBinarizer] = cluster_label_binarizer
        self.cluster_tfidf = cluster_tfidf
        self.mappings, self.clusters, self.multi_cluster = load_mappings(self.indexers_path, self.indexer)
        self.transformer_for_embedding = None
        Ranker.create_directories(models_path, indexer)

    @classmethod
    def create_directories(cls, models_path, indexer):
        create_dir_if_dont_exist(models_path / indexer)
        create_dir_if_dont_exist(models_path / indexer / "ranker")

    @classmethod
    def load(cls, indexers_path: Path, models_path: Path, indexer: str, load_from_gcp: bool = False):
        cls.create_directories(models_path, indexer)
        _, clusters, _ = load_mappings(indexers_path, indexer)
        cluster_classifier = {}
        cluster_label_binarizer = {}
        cluster_tfidf = {}
        for cluster in clusters:
            label_binarizer_path = models_path / indexer / "ranker" / f"label_binarizer_{cluster}.pickle"
            classifier_path = models_path / indexer / "ranker" / f"classifier_{cluster}.pickle"
            tfidf_path = models_path / indexer / "ranker" / f"tfidf_{cluster}.pickle"
            if load_from_gcp:
                download_blob_file(f"{indexer}/ranker/label_binarizer_{cluster}.pickle", label_binarizer_path)
                download_blob_file(f"{indexer}/ranker/classifier_{cluster}.pickle", classifier_path)
                download_blob_file(f"{indexer}/ranker/tfidf_{cluster}.pickle", tfidf_path)
            cluster_label_binarizer[cluster] = load_pickle(label_binarizer_path)
            try:
                cluster_classifier[cluster] = load_pickle(classifier_path)
                cluster_tfidf[cluster] = load_pickle(tfidf_path)
            except FileNotFoundError:
                cluster_classifier[cluster] = None
                cluster_tfidf[cluster] = None
        return cls(
            indexers_path,
            models_path,
            indexer,
            cluster_classifier=cluster_classifier,
            cluster_label_binarizer=cluster_label_binarizer,
            cluster_tfidf=cluster_tfidf,
        )

    def _read_sentences(
        self,
        cluster: str,
        split_types: List[str],
        augmentation: List[Augmentation] = [],
        use_incorrect_matcher_predictions: bool = False,
        subset: int = 0,
    ) -> List[Sentence]:
        filename = f"ranker_{cluster}"
        corpus = read_corpus(self.indexers_path / self.indexer / "ranker", filename)
        corpora = [corpus]
        incorrect_matcher_path = self.models_path / self.indexer / "incorrect-matcher"
        if use_incorrect_matcher_predictions and incorrect_matcher_path.exists():
            incorrect_matcher_corpus = read_corpus(incorrect_matcher_path, f"incorrect_{cluster}", only_train=True)
            corpora.append(incorrect_matcher_corpus)
        corpora += read_augmentation_corpora(augmentation, self.indexers_path, self.indexer, "ranker", cluster)
        multi_corpus = MultiCorpus(corpora)
        sentences = []
        for split_type in split_types:
            if getattr(multi_corpus, split_type):
                sentences += list(getattr(multi_corpus, split_type))
        if subset and len(sentences) < subset:
            sentences = sentences[:subset]
        return sentences

    def _set_multi_label_binarizer(self, cluster: str):
        mlb = MultiLabelBinarizer()
        labels_cluster = {
            f"<{label}>"
            for label in self.mappings.keys()
            if label_in_cluster(cluster, label, self.mappings, self.multi_cluster)
        }
        mlb.fit([list(labels_cluster) + ["<incorrect-matcher>"]])
        self.cluster_label_binarizer[cluster] = mlb

    def _set_tfidf(self, cluster: str, augmentation: List[Augmentation]):
        tfidf = TfidfVectorizer()
        tfidf.fit(
            s.to_original_text() for s in self._read_sentences(cluster, ["train", "dev"], augmentation=augmentation)
        )
        self.cluster_tfidf[cluster] = tfidf

    def _get_labels_matrix(self, cluster: str, sentences: List[Sentence]):
        mlb = self.cluster_label_binarizer[cluster]
        labels_matrix = mlb.transform([[label.value for label in s.get_labels("gold")] for s in sentences])
        return labels_matrix

    def _get_embeddings(self, cluster: str, sentences: List[Sentence], transformer_for_embedding: str = None):
        self.transformer_for_embedding = (
            TransformerDocumentEmbeddings(transformer_for_embedding, layers="-1,-2,-3,-4", fine_tune=False)
            if not self.transformer_for_embedding and transformer_for_embedding
            else None
        )
        embeddings = self.cluster_tfidf[cluster].transform(s.to_original_text() for s in sentences)
        if self.transformer_for_embedding:
            self.transformer_for_embedding.model.eval()
            for chunk in chunks(sentences):
                self.transformer_for_embedding.embed(chunk)
                for sentence in chunk:
                    sentence.embedding.to("cpu")
            transformer_embeddings = np.array([sentence.embedding.to("cpu").numpy() for sentence in sentences])
            embeddings = np.hstack((embeddings.toarray(), transformer_embeddings))
        print("Got embeddings")
        return embeddings

    def train(
        self,
        upload_to_gcp: bool = False,
        split_types_train: List[str] = ["train", "dev"],
        augmentation: List[Augmentation] = [
            Augmentation.ner_mention,
            Augmentation.ner_sentence,
            Augmentation.ner_stripped,
            Augmentation.descriptions_labels,
        ],
        use_incorrect_matcher_predictions: bool = False,
        subset: int = 0,
        transformer_for_embedding: str = None,
    ):
        len_clusters = len(self.clusters)
        for i, cluster in enumerate(self.clusters):
            print(f"Training for cluster {cluster} - {i}/{len_clusters}")
            self._set_multi_label_binarizer(cluster)
            sentences = self._read_sentences(
                cluster, split_types_train, augmentation, use_incorrect_matcher_predictions, subset
            )
            if not sentences:
                print("Cluster has no sentences")
                self.cluster_tfidf[cluster] = None
                self.cluster_classifier[cluster] = None
                continue
            self._set_tfidf(cluster, augmentation)
            embeddings = self._get_embeddings(cluster, sentences, transformer_for_embedding)
            labels = self._get_labels_matrix(cluster, sentences)

            clf = OneVsRestClassifier(XGBClassifier(eval_metric="logloss")).fit(embeddings, labels)
            self.cluster_classifier[cluster] = clf
        print("Training Complete")
        self.save()
        if upload_to_gcp:
            self.upload_to_gcp()

    def predict(self, sentences: List[Sentence], return_probabilities=True):
        print("Predicting ranker")
        for cluster in self.clusters:
            print(cluster)
            classes = self.cluster_label_binarizer[cluster].classes_
            classifier = self.cluster_classifier.get(cluster)
            if not classifier:
                for sentence in sentences:
                    for label in classes:
                        sentence.add_label("ranker_proba", label, 0.0)
            elif return_probabilities:
                embeddings = self._get_embeddings(cluster, sentences)
                predictions = classifier.predict_proba(embeddings)
                for sentence, prediction in zip(sentences, predictions):
                    for i, label in enumerate(classes):
                        sentence.add_label("ranker_proba", label, prediction[i])
            else:
                embeddings = self._get_embeddings(cluster, sentences)
                predictions = self.cluster_classifier[cluster].predict(embeddings)
                classes = self.cluster_label_binarizer[cluster].classes_
                for sentence, prediction in zip(sentences, predictions):
                    for i, pred in enumerate(prediction):
                        if pred:
                            sentence.add_label("ranker", classes[i], 1.0)

    def eval_weighted(
        self, split_types: List[str] = ["test"], eval_weighted_metrics: List[Metrics] = [Metrics.map, Metrics.summary]
    ):
        for metric in eval_weighted_metrics:
            print(f"Calculating {metric} weighted for {split_types}")
            metric_clusters = {}
            for split_type in split_types:
                for cluster in self.clusters:
                    sentences = self._read_sentences(cluster, [split_type])
                    if not sentences:
                        continue
                    classifier = self.cluster_classifier.get(cluster)
                    labels_matrix = self._get_labels_matrix(cluster, sentences)
                    if metric == Metrics.map:
                        if not classifier:
                            predictions = np.zeros_like(labels_matrix)
                            print(predictions.shape)
                        else:
                            embeddings = self._get_embeddings(cluster, sentences)
                            predictions = classifier.predict_proba(embeddings)
                        aps = []
                        for y_true, y_scores in zip(labels_matrix, predictions):
                            aps.append(average_precision_score(y_true, y_scores))
                            metric_clusters[cluster] = (statistics.mean(aps), embeddings.shape[0])
                    elif metric == Metrics.summary:
                        if not classifier:
                            predictions = np.zeros_like(labels_matrix)
                        else:
                            embeddings = self._get_embeddings(cluster, sentences)
                            predictions = self.cluster_classifier[cluster].predict(embeddings)
                        metric_clusters[cluster] = (
                            f1_score(labels_matrix, predictions, average="micro"),
                            embeddings.shape[0],
                        )
                weighted_metric = sum(map_stat * d_points for map_stat, d_points in metric_clusters.values()) / sum(
                    d_points for _, d_points in metric_clusters.values()
                )
            print(metric)
            print(metric_clusters)
            print(weighted_metric)

    def save(self):
        print("Saving model")
        base_path = self.models_path / self.indexer / "ranker"
        for cluster in self.clusters:
            save_as_pickle(self.cluster_label_binarizer[cluster], base_path / f"label_binarizer_{cluster}.pickle")
            save_as_pickle(self.cluster_classifier[cluster], base_path / f"classifier_{cluster}.pickle")
            save_as_pickle(self.cluster_tfidf[cluster], base_path / f"tfidf_{cluster}.pickle")

    def upload_to_gcp(self):
        base_path = self.models_path / self.indexer / "ranker"
        for cluster in self.clusters:
            upload_blob_file(
                base_path / f"label_binarizer_{cluster}.pickle",
                f"{self.indexer}/ranker/label_binarizer_{cluster}.pickle",
            )
            upload_blob_file(
                base_path / f"classifier_{cluster}.pickle", f"{self.indexer}/ranker/classifier_{cluster}.pickle"
            )
            upload_blob_file(base_path / f"tfidf_{cluster}.pickle", f"{self.indexer}/ranker/tfidf_{cluster}.pickle")
