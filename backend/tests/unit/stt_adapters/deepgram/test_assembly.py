"""Provider stream time -> capture timeline, and turn-window segment assembly (docs/07 §9, §11)."""

from __future__ import annotations

from voice_agent.contracts.stt import AudioWindow
from voice_agent.stt_adapters.deepgram.assembly import (
    MappedWord,
    Segment,
    SegmentAssembler,
    StreamTimeMap,
)


def _segment(
    start: int, end: int, words: list[tuple[str, int, int]], *, lang: str = "en"
) -> Segment:
    mapped = tuple(MappedWord(text, s, e, lang) for text, s, e in words)
    return Segment(
        start_ms=start,
        end_ms=end,
        text=" ".join(w[0] for w in words),
        words=mapped,
        languages=(lang,),
        confidence=0.9,
    )


def test_time_map_follows_contiguous_stream_offsets_across_capture_gaps() -> None:
    times = StreamTimeMap()
    times.record(captured_at_ms=1000, duration_ms=20)
    times.record(captured_at_ms=1020, duration_ms=20)
    times.record(captured_at_ms=5000, duration_ms=20)  # mic paused, stream offset continues

    assert times.sent_ms == 60
    assert times.to_capture_ms(0.0) == 1000
    assert times.to_capture_ms(0.030) == 1030
    assert times.to_capture_ms(0.045) == 5005
    assert times.to_capture_ms(10.0) == 5020  # beyond sent audio clamps to the end


def test_time_map_is_bounded_and_extrapolates_before_the_retained_range() -> None:
    times = StreamTimeMap(max_frames=3)
    for index in range(5):
        times.record(captured_at_ms=index * 20, duration_ms=20)

    assert times.to_capture_ms(0.0) == 0
    assert times.to_capture_ms(0.090) == 90


def test_segments_are_assembled_in_audio_time_order_not_arrival_order() -> None:
    assembler = SegmentAssembler()
    assembler.add(_segment(1000, 1500, [("world", 1000, 1400)]))
    assembler.add(_segment(0, 900, [("hello", 100, 800)]))

    taken = assembler.take(AudioWindow(start_ms=0, end_ms=2000))

    assert taken.text == "hello world"
    assert [s.start_ms for s in taken.segments] == [0, 1000]


def test_duplicate_segment_by_interval_and_text_fingerprint_is_ignored() -> None:
    assembler = SegmentAssembler()
    assert assembler.add(_segment(0, 900, [("hello", 100, 800)]))
    assert not assembler.add(_segment(0, 900, [("hello", 100, 800)]))

    assert assembler.take(None).text == "hello"
    assert assembler.duplicates == 1


def test_words_ending_before_the_window_are_excluded_and_later_words_kept_for_the_next_turn() -> (
    None
):
    assembler = SegmentAssembler()
    assembler.add(
        _segment(0, 3000, [("echo", 100, 400), ("mera", 1100, 1400), ("next", 2600, 2900)])
    )

    first = assembler.take(AudioWindow(start_ms=1000, end_ms=2000))
    second = assembler.take(AudioWindow(start_ms=2000, end_ms=3500))

    assert first.text == "mera"
    assert assembler.excluded_words == 1
    assert second.text == "next"


def test_segment_without_words_is_bound_by_interval_overlap() -> None:
    assembler = SegmentAssembler()
    assembler.add(Segment(start_ms=100, end_ms=900, text="haan", words=(), languages=("hi",)))

    assert assembler.take(AudioWindow(start_ms=2000, end_ms=3000)).text == ""
    assert assembler.take(AudioWindow(start_ms=0, end_ms=1000)).text == ""  # older than last window


def test_language_and_confidence_come_only_from_kept_evidence() -> None:
    assembler = SegmentAssembler()
    assembler.add(_segment(0, 500, [("मेरा", 0, 200), ("नाम", 250, 450)], lang="hi"))
    assembler.add(_segment(500, 900, [("Arun", 550, 850)], lang="en"))

    taken = assembler.take(None)

    assert taken.language == "hi"
    assert taken.confidence == 0.9
    empty = assembler.take(None)
    assert empty.text == ""
    assert empty.language is None
    assert empty.confidence is None


def test_pending_text_joins_unassigned_segments_for_partials() -> None:
    assembler = SegmentAssembler()
    assembler.add(_segment(0, 500, [("hello", 0, 400)]))

    assert assembler.pending_text() == "hello"
    assembler.take(None)
    assert assembler.pending_text() == ""


def test_assembler_is_bounded_and_counts_overflow() -> None:
    assembler = SegmentAssembler(max_segments=2)
    for index in range(4):
        assembler.add(
            _segment(
                index * 1000, index * 1000 + 500, [(f"w{index}", index * 1000, index * 1000 + 400)]
            )
        )

    assert assembler.overflowed == 2
    assert assembler.take(None).text == "w2 w3"
