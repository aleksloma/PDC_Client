"""Nothing a chart names is fetched when the server renders it to PNG.

`routes.report._plotly_html_to_png` runs kaleido's Chromium inside the web
container. Before `fig.to_image` every URL-bearing value is blanked: image
sources (layout.images, image traces), map layer sources and styles, a
geojson given as a URL, anything under a template — a `data:` URI survives.
The other callers (report PDF/PPTX, Auto Analytics) go through the same
function. `to_image` is stubbed: no Chromium starts here.
"""
import json

import plotly.graph_objects as go
import pytest

from routes import report

PNG = bytes([0x89, 0x50, 0x4E, 0x47]) + b"stub"
DATA_URI = "data:image/png;base64,iVBORw0KGgo="


def _html(data, layout):
    return ('<div id="c"></div><script>Plotly.newPlot("c", '
            + json.dumps(data) + ", " + json.dumps(layout) + ", {})</script>")


@pytest.fixture
def rendered(monkeypatch):
    seen = []

    def fake(self, *a, **k):
        seen.append(self.to_dict())
        return PNG

    monkeypatch.setattr(go.Figure, "to_image", fake)
    return seen


def _all_strings(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _all_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_strings(v)
    elif isinstance(obj, str):
        yield obj


@pytest.mark.parametrize("source", [
    "http://127.0.0.1:9/probe.png", "https://169.254.169.254/latest/meta-data",
    "file:///etc/passwd", "//evil.example/x.png", "FILE:///app/app.py",
])
def test_layout_image_sources_are_blanked(rendered, source):
    html = _html([{"type": "bar", "x": [1], "y": [1]}],
                 {"images": [{"source": source, "x": 0, "y": 0, "sizex": 1, "sizey": 1}]})
    assert report._plotly_html_to_png(html, "t") == PNG
    strings = list(_all_strings(rendered[-1]))
    assert source not in strings


def test_a_data_uri_survives(rendered):
    html = _html([{"type": "bar", "x": [1], "y": [1]}],
                 {"images": [{"source": DATA_URI, "x": 0, "y": 0}]})
    report._plotly_html_to_png(html, "t")
    assert rendered[-1]["layout"]["images"][0]["source"] == DATA_URI


def test_an_image_trace_source_is_blanked(rendered):
    html = _html([{"type": "image", "source": "http://10.0.0.5/secret.png"}], {})
    report._plotly_html_to_png(html, "t")
    assert "http://10.0.0.5/secret.png" not in list(_all_strings(rendered[-1]))


def test_map_styles_layers_and_tokens_are_neutralised():
    layout = {"mapbox": {"style": "https://tiles.example/style.json", "accesstoken": "pk.x",
                         "layers": [{"source": "http://10.0.0.1/geo.json", "type": "fill"}]},
              "map2": {"style": "open-street-map"}}
    data = [{"type": "choropleth", "geojson": "http://10.0.0.2/shapes.json"}]
    count = report._strip_remote_refs(data) + report._strip_remote_refs(layout)
    assert count == 5
    assert layout["mapbox"]["style"] == "white-bg"
    assert layout["map2"]["style"] == "white-bg"
    assert layout["mapbox"]["accesstoken"] == ""
    assert layout["mapbox"]["layers"][0]["source"] == ""
    assert data[0]["geojson"] == ""


def test_a_template_is_walked_too():
    layout = {"template": {"layout": {"images": [{"source": "http://x/y.png"}]}}}
    assert report._strip_remote_refs(layout) == 1
    assert layout["template"]["layout"]["images"][0]["source"] == ""


def test_ordinary_text_that_looks_like_a_url_is_left_alone():
    """Only URL-bearing KEYS are touched: a category label or a title that
    happens to read like a URL is data, and the chart must keep it."""
    data = [{"type": "bar", "x": ["http://a.example", "b"], "y": [1, 2]}]
    layout = {"title": {"text": "see http://a.example"}}
    assert report._strip_remote_refs(data) + report._strip_remote_refs(layout) == 0
    assert data[0]["x"][0] == "http://a.example"


def test_a_geojson_object_is_kept():
    data = [{"type": "choropleth", "geojson": {"type": "FeatureCollection", "features": []}}]
    assert report._strip_remote_refs(data) == 0


def test_a_map_style_object_with_its_own_urls_becomes_white_bg():
    layout = {"mapbox": {"style": {"version": 8,
                                   "sources": {"osm": {"type": "raster",
                                                       "tiles": ["http://127.0.0.1/{z}/{x}/{y}.png"]}},
                                   "sprite": "http://127.0.0.1/sprite",
                                   "glyphs": "http://127.0.0.1/{fontstack}/{range}.pbf",
                                   "layers": []}}}
    assert report._strip_remote_refs(layout) == 1
    assert layout["mapbox"]["style"] == "white-bg"


def test_a_list_of_tile_urls_is_emptied():
    layout = {"mapbox": {"layers": [{"sourcetype": "raster",
                                     "source": ["http://127.0.0.1/{z}/{x}/{y}.png",
                                                "https://tiles.example/{z}/{x}/{y}.png"]}]}}
    data = [{"type": "choroplethmapbox", "geojson": ["http://x/a.json"]}]
    count = report._strip_remote_refs(layout) + report._strip_remote_refs(data)
    assert count == 2
    assert layout["mapbox"]["layers"][0]["source"] == []
    assert data[0]["geojson"] == []


@pytest.mark.parametrize("text", [
    '<span style="background-image:url(http://127.0.0.1/x.png)">x</span>',
    "<b style='cursor:url(file:///etc/passwd), auto'>x</b>",
    '<span  STYLE = "background:url(//evil/x)" >x</span>',
    '<i style=background:url(http://a/b)>x</i>',
    # plotly.js also honours a style attribute directly after a quote
    '<span title="t"style="background:url(http://127.0.0.1:9/q.png)">x</span>',
    "<b title='t'style='background:url(http://127.0.0.1:9/q2.png)'>x</b>",
])
def test_pseudo_html_style_attributes_are_removed_from_text(text):
    layout = {"annotations": [{"text": text}], "title": {"text": text},
              "xaxis": {"ticktext": [text, "plain"]}}
    report._strip_remote_refs(layout)
    for value in (layout["annotations"][0]["text"], layout["title"]["text"],
                  layout["xaxis"]["ticktext"][0]):
        assert "url(" not in value.lower(), value
        assert "style" not in value.lower(), value
        assert value.endswith(">x</span>") or value.endswith(">x</b>") or value.endswith(">x</i>")
    assert layout["xaxis"]["ticktext"][1] == "plain"
