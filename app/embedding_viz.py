"""3D projection of a conversation's question embeddings vs. retrieved chunks.

Reduces the dense vectors to 3D with PCA (fast, deterministic, no
per-query tuning) and plots them with Plotly. Every question asked so far in
the conversation is tracked as a point (with a trajectory line showing how
the conversation has moved through the embedding space); only the most
recent question's retrieved chunks and background sample are shown, exactly
as before.
"""

import random

import plotly.graph_objects as go
from sklearn.decomposition import PCA

from common import config, storage

BACKGROUND_SAMPLE_SIZE = 150
MIN_POINTS_FOR_PCA = 3

# Blue palette, matching the web app's theme (web/static/css/app.css)
_LIGHT = "#EFF4FF"
_VERY_LIGHT = "#DBE7FF"
_BLUE = "#3155D9"
_BRIGHT = "#2563EB"
_DEEP_BLUE = "#172554"
_NAVY = "#0F172A"

# Score colorscale built from the same six colors. Dark theme, so it runs
# dark -> bright: low-relevance points blend into the navy background,
# high-relevance points glow brightest.
_SCORE_COLORSCALE = [
    [0.0, _DEEP_BLUE],
    [0.2, _BLUE],
    [0.45, _BRIGHT],
    [0.65, _VERY_LIGHT],
    [1.0, _LIGHT],
]


def _fetch_background_vectors(
    exclude_ids: set[str], vector_name: str, sample_size: int = BACKGROUND_SAMPLE_SIZE
) -> list[list[float]]:
    client = storage.get_client()
    points = storage.background_vectors(client, sample_size * 2)
    vectors = [
        point["vector"]
        for point in points
        if point["id"] not in exclude_ids
    ]
    if len(vectors) > sample_size:
        vectors = random.sample(vectors, sample_size)
    return vectors


def build_embedding_plot(
    question_history: list[dict],
    chunks: list[dict],
    vector_name: str = config.DENSE_VECTOR_NAME,
    vector_key: str = "vector",
) -> go.Figure | None:
    """question_history: chronological list of {"question": str, vector_key: [...]},
    the last entry being the current question; entries without that key
    (asked before this model was available) are left out. chunks: retrieved
    for the current (last) question only, carrying vector_name's vectors."""
    question_history = [q for q in question_history if q.get(vector_key) is not None]
    if not question_history:
        return None

    retrieved_vectors = [c["vector"] for c in chunks if c.get("vector") is not None]
    retrieved_ids = {c["id"] for c in chunks}
    background = _fetch_background_vectors(retrieved_ids, vector_name)
    question_vectors = [q[vector_key] for q in question_history]

    all_vectors = background + retrieved_vectors + question_vectors
    if len(all_vectors) < MIN_POINTS_FOR_PCA:
        return None

    coords = PCA(n_components=3).fit_transform(all_vectors)

    n_background = len(background)
    n_retrieved = len(retrieved_vectors)
    bg_coords = coords[:n_background]
    retrieved_coords = coords[n_background : n_background + n_retrieved]
    question_coords = coords[n_background + n_retrieved :]

    fig = go.Figure()

    if len(bg_coords):
        fig.add_trace(
            go.Scatter3d(
                x=bg_coords[:, 0],
                y=bg_coords[:, 1],
                z=bg_coords[:, 2],
                mode="markers",
                marker=dict(size=3, color="rgba(239,244,255,0.18)"),
                name="Other chunks",
                hoverinfo="skip",
            )
        )

    if n_retrieved:
        scores = [chunks[i]["score"] for i in range(n_retrieved)]
        score_min, score_max = min(scores), max(scores)
        if score_max == score_min:
            score_max = score_min + 1e-6

        hover_text = [
            f"#{i + 1} · score {chunks[i]['score']:.3f}<br>{chunks[i]['source_url']}<br>"
            f"{chunks[i]['text'][:120]}…"
            for i in range(n_retrieved)
        ]
        fig.add_trace(
            go.Scatter3d(
                x=retrieved_coords[:, 0],
                y=retrieved_coords[:, 1],
                z=retrieved_coords[:, 2],
                mode="markers+text",
                marker=dict(
                    size=9,
                    color=scores,
                    colorscale=_SCORE_COLORSCALE,
                    cmin=score_min,
                    cmax=score_max,
                    showscale=True,
                    colorbar=dict(
                        title=dict(text="Score", font=dict(color=_LIGHT)),
                        thickness=14,
                        len=0.5,
                        x=1.0,
                        tickfont=dict(color=_LIGHT),
                    ),
                    line=dict(width=1, color=_LIGHT),
                ),
                text=[f"#{i + 1}" for i in range(n_retrieved)],
                textposition="top center",
                textfont=dict(color=_LIGHT, size=12),
                name="Retrieved chunks",
                hovertext=hover_text,
                hovertemplate="%{hovertext}<extra></extra>",
            )
        )

    n_questions = len(question_coords)

    # Trajectory line through every question asked so far, in order.
    if n_questions > 1:
        fig.add_trace(
            go.Scatter3d(
                x=question_coords[:, 0],
                y=question_coords[:, 1],
                z=question_coords[:, 2],
                mode="lines",
                line=dict(color="rgba(49,85,217,0.55)", width=3, dash="dot"),
                name="Question path",
                hoverinfo="skip",
                showlegend=False,
            )
        )

        past_coords = question_coords[:-1]
        past_hover = [f"Q{i + 1}: {question_history[i]['question']}" for i in range(n_questions - 1)]
        fig.add_trace(
            go.Scatter3d(
                x=past_coords[:, 0],
                y=past_coords[:, 1],
                z=past_coords[:, 2],
                mode="markers+text",
                marker=dict(size=6, color="rgba(49,85,217,0.55)", symbol="diamond"),
                text=[f"Q{i + 1}" for i in range(n_questions - 1)],
                textposition="top center",
                textfont=dict(size=9, color="rgba(239,244,255,0.6)"),
                name="Earlier questions",
                hovertext=past_hover,
                hovertemplate="%{hovertext}<extra></extra>",
            )
        )

    current_coord = question_coords[-1]
    fig.add_trace(
        go.Scatter3d(
            x=[current_coord[0]],
            y=[current_coord[1]],
            z=[current_coord[2]],
            mode="markers+text",
            marker=dict(size=11, color=_BRIGHT, symbol="diamond", line=dict(width=1, color=_LIGHT)),
            text=[f"Q{n_questions}"] if n_questions > 1 else [""],
            textposition="top center",
            textfont=dict(color=_LIGHT),
            name="Current question",
            hovertext=[f"Current: {question_history[-1]['question']}"],
            hovertemplate="%{hovertext}<extra></extra>",
        )
    )

    axis_style = dict(
        backgroundcolor=_DEEP_BLUE,
        gridcolor=_BLUE,
        zerolinecolor=_BRIGHT,
        color=_LIGHT,
    )
    fig.update_layout(
        scene=dict(
            xaxis=dict(title="PC1", **axis_style),
            yaxis=dict(title="PC2", **axis_style),
            zaxis=dict(title="PC3", **axis_style),
        ),
        paper_bgcolor=_DEEP_BLUE,
        font=dict(color=_LIGHT),
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, font=dict(color=_LIGHT)),
        height=560,
    )
    return fig
