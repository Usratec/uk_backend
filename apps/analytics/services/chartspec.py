BLUE = "#4a58d1"
GREEN = "#5aa86f"
RED = "#c64a45"
ORANGE = "#d2652d"
GREY = "#8b8fae"
LAVENDER = "#c9cdf0"

BLUE_FILL = "rgba(74, 88, 209, 0.18)"
GREEN_FILL = "rgba(90, 168, 111, 0.18)"


def line(
    label: str,
    data: list,
    colour: str = BLUE,
    *,
    fill: str | None = None,
    dashed: bool = False,
    span_gaps: bool = False,
) -> dict:
    dataset = {
        "label": label,
        "data": data,
        "borderColor": colour,
        "backgroundColor": fill or colour,
        "borderWidth": 2,
        "pointRadius": 3,
        "pointBackgroundColor": colour,
        "tension": 0,
        "fill": bool(fill),
        "spanGaps": span_gaps,
    }
    if dashed:
        dataset["borderDash"] = [6, 4]
    return dataset


def bars(label: str, data: list, colour: str, **extra) -> dict:
    return {"label": label, "data": data, "backgroundColor": colour, "borderColor": colour, **extra}


def chart(kind: str, labels: list[str], datasets: list[dict], **options) -> dict:
    return {"type": kind, "labels": labels, "datasets": datasets, **options}


def has_data(datasets: list[dict]) -> bool:
    return any(any(value for value in dataset.get("data", [])) for dataset in datasets)
