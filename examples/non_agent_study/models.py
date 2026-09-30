"""Small actual learned CPU models; parameters and transforms are serialized as JSON."""

from __future__ import annotations

import numpy as np

from examples.non_agent_study.data import digest


def _matrix(rows, indices):
    values = np.asarray(
        [[row["features"][index] for index in indices] for row in rows], dtype=np.float64
    )
    return values


def fit_scaler(values, *, missing_indicators):
    finite = np.isfinite(values)
    counts = finite.sum(axis=0)
    mean = np.divide(
        np.where(finite, values, 0).sum(axis=0),
        counts,
        out=np.zeros(values.shape[1]),
        where=counts > 0,
    )
    filled = np.where(finite, values, mean)
    scale = filled.std(axis=0)
    scale[scale == 0] = 1
    return {
        "mean": mean.tolist(),
        "scale": scale.tolist(),
        "missing_indicators": missing_indicators,
    }


def transform(values, scaler):
    missing = ~np.isfinite(values)
    normalized = (
        np.where(missing, np.asarray(scaler["mean"]), values) - np.asarray(scaler["mean"])
    ) / np.asarray(scaler["scale"])
    return (
        np.concatenate([normalized, missing.astype(np.float64)], axis=1)
        if scaler["missing_indicators"]
        else normalized
    )


def train(rows, *, algorithm, feature_indices=None, missing_indicators=False):
    if not rows or algorithm not in {"knn", "centroid", "logistic"}:
        raise ValueError("nonempty training rows and a supported algorithm are required")
    indices = (
        list(range(len(rows[0]["features"]))) if feature_indices is None else list(feature_indices)
    )
    raw = _matrix(rows, indices)
    scaler = fit_scaler(raw, missing_indicators=missing_indicators)
    values = transform(raw, scaler)
    labels = sorted({row["label"] for row in rows})
    if len(labels) < 2:
        raise ValueError("study classifiers require at least two training classes")
    encoded = np.asarray([labels.index(row["label"]) for row in rows], dtype=np.int64)
    parameters = {
        "schema_version": "1.0",
        "algorithm": algorithm,
        "version": "1.0",
        "labels": labels,
        "feature_indices": indices,
        "scaler": scaler,
        "numpy_version": np.__version__,
        "training_rows_sha256": digest(rows),
        "training_count": len(rows),
    }
    if algorithm == "knn":
        parameters.update(k=5, vectors=values.tolist(), targets=encoded.tolist())
    elif algorithm == "centroid":
        parameters["centers"] = [
            values[encoded == index].mean(axis=0).tolist() for index in range(len(labels))
        ]
    else:
        if len(labels) != 2:
            raise ValueError("logistic matcher requires exactly two labels")
        design = np.column_stack([values, np.ones(len(values))])
        weights = np.zeros(design.shape[1])
        counts = np.bincount(encoded, minlength=2)
        row_weights = np.asarray([len(rows) / (2 * counts[index]) for index in encoded])
        for _ in range(400):
            score = design @ weights
            probability = 1 / (1 + np.exp(-np.clip(score, -40, 40)))
            gradient = design.T @ ((probability - encoded) * row_weights) / len(rows)
            gradient[:-1] += 0.001 * weights[:-1]
            weights -= 0.1 * gradient
        parameters.update(
            weights=weights.tolist(),
            iterations=400,
            learning_rate=0.1,
            l2=0.001,
            decision_threshold=0.5,
        )
    parameters["sha256"] = digest(parameters)
    return parameters


class Model:
    def __init__(self, parameters):
        owned = dict(parameters)
        if owned.pop("sha256", None) != digest(owned):
            raise ValueError("model parameter hash mismatch")
        self.parameters = parameters
        self.algorithm = parameters["algorithm"]
        self.indices = parameters["feature_indices"]
        self.labels = parameters["labels"]
        self.scaler = parameters["scaler"]
        self.vectors = np.asarray(parameters.get("vectors", []), dtype=np.float64)
        self.targets = np.asarray(parameters.get("targets", []), dtype=np.int64)
        self.centers = np.asarray(parameters.get("centers", []), dtype=np.float64)
        self.weights = np.asarray(parameters.get("weights", []), dtype=np.float64)

    def predict(self, rows):
        if not rows:
            return []
        values = transform(_matrix(rows, self.indices), self.scaler)
        if self.algorithm == "logistic":
            logits = np.column_stack([values, np.ones(len(values))]) @ self.weights
            positive = 1 / (1 + np.exp(-np.clip(logits, -40, 40)))
            predictions = (positive >= self.parameters["decision_threshold"]).astype(int)
            confidence = np.abs(positive - 0.5) * 2
        elif self.algorithm == "centroid":
            distances = ((values[:, None, :] - self.centers[None, :, :]) ** 2).sum(axis=2)
            ordered = np.argsort(distances, axis=1, kind="stable")
            predictions = ordered[:, 0]
            first = np.take_along_axis(distances, ordered[:, :2], axis=1)
            confidence = (first[:, 1] - first[:, 0]) / np.maximum(first[:, 1], 1e-12)
        else:
            distances = ((values[:, None, :] - self.vectors[None, :, :]) ** 2).sum(axis=2)
            nearest = np.argsort(distances, axis=1, kind="stable")[:, : self.parameters["k"]]
            counts = np.stack(
                [(self.targets[nearest] == index).sum(axis=1) for index in range(len(self.labels))],
                axis=1,
            )
            predictions = counts.argmax(axis=1)
            confidence = counts.max(axis=1) / nearest.shape[1]
        return [
            {"label": self.labels[int(label)], "confidence": float(certainty)}
            for label, certainty in zip(predictions, confidence)
        ]
