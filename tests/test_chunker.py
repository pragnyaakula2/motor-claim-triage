from claimtriage.retrieval.chunker import dedouble_line, detect_heading, extract_refs


def test_dedouble():
    assert dedouble_line("BBooookk  YYoouurr  2200||2200  SSlloott") == "Book Your 20|20 Slot"
    assert dedouble_line("Normal sentence stays intact.") == "Normal sentence stays intact."


def test_headings():
    assert detect_heading("GENERAL EXCEPTIONS (Applicable to all sections of the Policy) The Company shall not")[1] == "general exceptions"
    assert detect_heading("SECTION II - LIABILITY TO THIRD PARTIES 1. Subject")[1] == "section ii "
    assert detect_heading("IMT.31. RELIABILITY TRIALS AND RALLIES (Private Cars")[1] == "imt.31"
    assert detect_heading("The Company will indemnify the insured") is None


def test_refs():
    assert "Limitations as to Use" in extract_refs("being used otherwise than in accordance with the 'Limitations as to Use'")


def test_ge_chunk_is_exclusion_with_unresolved_use_ref(index):
    ge = index.by_id["PCPP:GE:01"]
    assert "exclusion" in ge["roles"] and "otherwise than in accordance with" in ge["text"]
    refs = {r["phrase"]: r for r in index.cross_refs("PCPP:GE:01")["refs"]}
    assert refs["Limitations as to Use"]["status"] == "unresolved"


def test_no_chunk_straddles_sections(index):
    assert all(c["section"] for c in index.chunks)
    assert not any("GENERAL EXCEPTIONS" in c["text"] and c["section_key"] != "general exceptions" for c in index.chunks)


def test_cover_clause_implicitly_linked_to_exceptions(index):
    refs = index.cross_refs("PCPP:SI:01")["refs"]
    implicit = [r for r in refs if "implicit" in r["phrase"]][0]
    assert "PCPP:GE:01" in implicit["resolved_to"]


def test_single_pass_misses_the_restriction_but_derived_query_finds_it(index):
    naive = {r["clause_id"] for r in index.search("I was making a food delivery run in my own car when the accident happened", 5)}
    derived = {r["clause_id"] for r in index.search("Limitations as to Use hire or reward vehicle used otherwise than in accordance with", 3, "exclusion")}
    assert "PCPP:GE:01" not in naive          # the structural problem in section 5 of the brief
    assert "PCPP:GE:01" in derived


def test_idempotent_rebuild_reembeds_nothing(index_dir):
    from claimtriage.pipeline.build import run
    m = run()
    assert m["reembedded_chunks"] == 0 and m["reused_chunks"] == m["chunk_count"]


def test_force_source_reembeds_only_that_source(index_dir):
    from claimtriage.pipeline.build import run
    m = run(force_sources=["Motor Insurance Claims.pdf"])
    n_claims = [s for s in m["sources"] if s["source"].startswith("Motor")][0]["chunks"]
    assert m["reembedded_chunks"] == n_claims
