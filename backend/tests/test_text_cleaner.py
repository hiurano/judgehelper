"""
Unit tests for text processing and cleaning service.
"""
from backend.services.text_cleaner import clean_transcript, format_metadata_block


def test_clean_transcript_legal_codes():
    raw_text = "Подсудимый обвиняется по ст. 228 кровного кодекса и У КРС."
    cleaned = clean_transcript(raw_text)
    assert "Уголовного кодекса" in cleaned
    assert "УК РФ" in cleaned


def test_clean_transcript_typos():
    raw_text = "Заседание проходило в городском суде г. Нижневатовский."
    cleaned = clean_transcript(raw_text)
    assert "Нижневартовский" in cleaned


def test_clean_transcript_empty():
    assert clean_transcript("") == ""
    assert clean_transcript(None) is None


def test_format_metadata_block():
    meta = {"defendant": "Иванов Иван Иванович"}
    block = format_metadata_block(meta)
    assert "Иванов Иван Иванович" in block
    assert "ИЗВЕСТНЫЕ ДАННЫЕ ДЕЛА" in block

    assert format_metadata_block({}) == ""
