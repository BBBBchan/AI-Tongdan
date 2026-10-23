import pytest

from clipper.live_publishing import check_evidence_quote


def captions():
    return [
        {"id": 12, "text": "我填叫轨划员", "start": 10.0, "end": 11.5},
        {"id": 13, "text": "天哪为什么还要讲话呢", "start": 11.6, "end": 14.0},
        {"id": 14, "text": "刚才那句不算", "start": 14.1, "end": 16.0},
    ]


def test_verbatim_sentence_can_cross_adjacent_subtitle_lines():
    check_evidence_quote([12, 13], "我填叫轨划员天哪为什么还要讲话呢", captions())
    check_evidence_quote([12, 13], "轨划员天哪为什么", captions())


@pytest.mark.parametrize("separator", [" ", "\n"])
def test_only_subtitle_boundary_separator_can_be_added(separator):
    check_evidence_quote([12, 13], "轨划员" + separator + "天哪为什么", captions())


@pytest.mark.parametrize("ids,quote", [
    ([12, 14], "轨划员刚才那句"),  # Omits actual speech in cue 13.
    ([13, 12], "讲话呢我填叫"),  # Reorders speech.
    ([12, 13], "我填叫规划员天哪"),  # Invents a correction to the ASR.
    ([12, 13], "我填叫轨划员为什么"),  # Removes words at the line break.
    ([12, 12], "轨划员我填叫"),
    ([12, 99], "轨划员天哪"),
    ([True], "我填叫"),
])
def test_quote_cannot_be_manufactured_by_splicing(ids, quote):
    with pytest.raises(ValueError, match="editorial_evidence_invalid"):
        check_evidence_quote(ids, quote, captions())


@pytest.mark.parametrize("changes", [
    {"start": 20.0, "end": 22.0},  # A long pause is not a subtitle line break.
    {"start": 9.0, "end": 12.0},
    {"start": None},
    {"end": float("nan")},
])
def test_cross_line_quote_requires_continuous_valid_timing(changes):
    rows = captions()
    rows[1].update(changes)
    with pytest.raises(ValueError, match="editorial_evidence_invalid"):
        check_evidence_quote([12, 13], "轨划员天哪为什么", rows)


def test_single_cue_legacy_evidence_still_works_without_timestamps():
    check_evidence_quote([46, 83], "阀门是个奔驰。", [
        {"id": 46, "text": "阀门是个奔驰。"}, {"id": 83, "text": "开起来。"},
    ])
