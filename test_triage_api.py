from __future__ import annotations

import asyncio
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, patch

from db import TriageDB, normalize_release_key
from gemini_client import build_user_prompt
from schemas import (
    DecisionAid,
    EvaluateRequest,
    EvaluationPayload,
    Identification,
    Ratings,
    Recognition,
)
from tmdb_client import (
    TmdbClient,
    TmdbMovie,
    apply_tmdb_verification,
    extract_tmdb_query,
    select_best_tmdb_candidate,
)


def _sample_payload(
    *,
    title: str = "La Chimera",
    original_title: str | None = None,
    year: int = 2023,
    director: str = "Alice Rohrwacher",
    imdb_id: str | None = None,
    tmdb_id: int | None = None,
    runtime_minutes: int | None = None,
    genres: list[str] | None = None,
) -> EvaluationPayload:
    return EvaluationPayload(
        identification=Identification(
            canonical_title=title,
            original_title=original_title,
            year=year,
            director=director,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            runtime_minutes=runtime_minutes,
            genres=genres if genres is not None else ["Drama"],
            match_confidence="high",
            disambiguation_note=None,
        ),
        ratings=Ratings(
            rotten_tomatoes_critics=95,
            rotten_tomatoes_audience=74,
            metacritic=91,
            imdb=7.3,
            letterboxd=4.1,
            sources={
                "rotten_tomatoes_critics": "rottentomatoes.com",
                "rotten_tomatoes_audience": "rottentomatoes.com",
                "metacritic": "metacritic.com",
                "imdb": "imdb.com",
                "letterboxd": "letterboxd.com",
            },
            missing_sources=[],
        ),
        recognition=Recognition(
            awards_highlights=["Cannes Palme d'Or Nominee"],
            critic_consensus="Rich folkloric fable with tactile craft.",
        ),
        decision_aid=DecisionAid(
            one_line_pitch="An archaeologist joins Tuscan tomb raiders.",
            similar_to=["Happy as Lazzaro", "The Wonders"],
            red_flags=["Leisurely pacing"],
            verdict="WATCH",
            verdict_reason="Top-tier arthouse acclaim and originality.",
        ),
    )


class TestTmdbAndEvaluate(unittest.TestCase):
    def test_extract_tmdb_query_handles_foreign_release_and_hyphens(self) -> None:
        title, year = extract_tmdb_query(
            "Anatomie.d.une.chute.2023.FRENCH.2160p.UHD.BluRay.HEVC-GROUP"
        )
        self.assertEqual(title, "Anatomie d une chute")
        self.assertEqual(year, 2023)

        title2, year2 = extract_tmdb_query("Spider-Man: Across the Spider-Verse", hint_year=2023)
        self.assertEqual(title2, "Spider-Man Across the Spider-Verse")
        self.assertEqual(year2, 2023)

    def test_select_best_tmdb_candidate_prefers_exact_year_and_foreign_original_title(self) -> None:
        results = [
            {
                "id": 100,
                "title": "Anatomy of a Fall",
                "original_title": "Anatomie d'une chute",
                "release_date": "2023-08-23",
                "popularity": 20.0,
            },
            {
                "id": 999,
                "title": "Anatomy of a Fall",
                "original_title": "Anatomy of a Fall",
                "release_date": "1998-05-01",
                "popularity": 50.0,
            },
        ]
        best = select_best_tmdb_candidate(results, "Anatomie d une chute", 2023)
        self.assertIsNotNone(best)
        assert best is not None
        self.assertEqual(best["id"], 100)

        # Remake year mismatch should be rejected
        miss = select_best_tmdb_candidate(results, "Anatomie d une chute", 2010)
        self.assertIsNone(miss)

    def test_apply_tmdb_verification_fills_deterministic_fields(self) -> None:
        payload = _sample_payload(
            title="Anatomy of a Fall",
            original_title=None,
            year=2023,
            director="Justine Triet",
            imdb_id=None,
            runtime_minutes=None,
            genres=[],
        )
        tmdb = TmdbMovie(
            tmdb_id=915935,
            canonical_title="Anatomy of a Fall",
            original_title="Anatomie d'une chute",
            year=2023,
            imdb_id="tt17009710",
            runtime_minutes=151,
            director="Justine Triet",
            genres=["Thriller", "Mystery", "Crime"],
            original_language="fr",
        )
        merged = apply_tmdb_verification(payload, tmdb)
        self.assertEqual(merged.identification.tmdb_id, 915935)
        self.assertEqual(merged.identification.imdb_id, "tt17009710")
        self.assertEqual(merged.identification.runtime_minutes, 151)
        self.assertEqual(merged.identification.original_title, "Anatomie d'une chute")
        self.assertEqual(merged.identification.genres, ["Thriller", "Mystery", "Crime"])

    def test_apply_tmdb_verification_skips_incompatible_movie(self) -> None:
        payload = _sample_payload(
            title="La Chimera",
            year=2023,
            director="Alice Rohrwacher",
            imdb_id="tt14561712",
            runtime_minutes=133,
        )
        wrong_tmdb = TmdbMovie(
            tmdb_id=12345,
            canonical_title="Chimera",
            original_title="Chimera",
            year=2018,
            imdb_id="tt99999999",
            runtime_minutes=95,
            director="Someone Else",
            genres=["Sci-Fi"],
        )
        merged = apply_tmdb_verification(payload, wrong_tmdb)
        self.assertIsNone(merged.identification.tmdb_id)
        self.assertEqual(merged.identification.imdb_id, "tt14561712")
        self.assertEqual(merged.identification.runtime_minutes, 133)

    def test_tmdb_client_auth_mode_and_fail_open(self) -> None:
        v3 = TmdbClient(api_key="0123456789abcdef0123456789abcdef")
        self.assertTrue(v3.enabled)
        self.assertFalse(v3._is_bearer_token())

        v4 = TmdbClient(api_key="eyJhbGciOiJIUzI1NiJ9." + "a" * 80)
        self.assertTrue(v4._is_bearer_token())

        disabled = TmdbClient(api_key="")
        self.assertFalse(disabled.enabled)
        self.assertIsNone(
            asyncio.run(disabled.lookup_from_request(EvaluateRequest(raw_text="La Chimera 2023")))
        )

        # Fail-open on network errors
        with patch.object(v3, "_lookup_from_request_sync", side_effect=urllib.error.URLError("down")):
            res = asyncio.run(v3.lookup_from_request(EvaluateRequest(raw_text="La Chimera 2023")))
            self.assertIsNone(res)

    def test_build_user_prompt_includes_tmdb_context(self) -> None:
        req = EvaluateRequest(raw_text="La.Chimera.2023.1080p.BluRay.x264-GROUP")
        tmdb = TmdbMovie(
            tmdb_id=837335,
            canonical_title="La Chimera",
            original_title="La chimera",
            year=2023,
            imdb_id="tt14561712",
            runtime_minutes=133,
            director="Alice Rohrwacher",
            genres=["Drama", "Comedy"],
        )
        prompt = build_user_prompt(req, tmdb_context=tmdb.to_prompt_block())
        self.assertIn("TMDB ID: 837335", prompt)
        self.assertIn("IMDb ID: tt14561712", prompt)
        self.assertIn("Runtime: 133 minutes", prompt)

    def test_db_release_key_and_cross_language_imdb_cache(self) -> None:
        self.assertEqual(
            normalize_release_key("La.Chimera.2023.1080p.BluRay.x264-GROUP"),
            "la chimera 2023",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TriageDB(Path(tmpdir) / "triage.db")
            db.init_db()
            payload = _sample_payload(
                title="Anatomy of a Fall",
                original_title="Anatomie d'une chute",
                year=2023,
                director="Justine Triet",
                imdb_id="tt17009710",
                tmdb_id=915935,
                runtime_minutes=151,
            ).model_dump()
            db.save_evaluation("Anatomie.d.une.chute.2023.FRENCH.2160p.UHD.BluRay.REMUX.DV", payload)

            # Different language title of the SAME quality tier hits cache via IMDb ID
            hit = db.get_cached_evaluation(
                "Anatomy.of.a.Fall.2023.2160p.UHD.BluRay.REMUX.DoVi-GRP",
                hint_title="Anatomy of a Fall",
                hint_year=2023,
                extra_context="tt17009710",
            )
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertEqual(hit["identification"]["tmdb_id"], 915935)
            self.assertEqual(hit["identification"]["imdb_id"], "tt17009710")

            # Different quality tier (1080p SDR vs 2160p DV) must NOT share cached verdict
            miss = db.get_cached_evaluation(
                "Anatomy.of.a.Fall.2023.1080p.BluRay.REMUX-GRP",
                hint_title="Anatomy of a Fall",
                hint_year=2023,
                extra_context="tt17009710",
            )
            self.assertIsNone(miss)

    def test_parse_release_quality_signals(self) -> None:
        from db import parse_release_quality

        q_4k_dv = parse_release_quality(
            "28 Years Later 2025 2160p UHD BluRay REMUX DV HDR HEVC Atmos-TRiToN",
            resolution="2160p",
            source="UHD.BluRay",
            dynamic_range=["DV", "HDR"],
            media_processing="REMUX",
        )
        self.assertEqual(q_4k_dv.format_label, "Remux-2160p")
        self.assertTrue(q_4k_dv.dolby_vision)
        self.assertEqual(q_4k_dv.cache_key, "2160p_remux_dv")
        self.assertIn("Dolby Vision bonus", q_4k_dv.to_prompt_line() or "")

        q_1080_sdr = parse_release_quality(
            "28 Years Later 2025 1080p BluRay Hybrid REMUX AVC Atmos-TRiToN"
        )
        self.assertEqual(q_1080_sdr.format_label, "Remux-1080p")
        self.assertFalse(q_1080_sdr.dolby_vision)
        self.assertEqual(q_1080_sdr.hdr_label, "SDR")
        self.assertEqual(q_1080_sdr.cache_key, "1080p_remux_sdr")

        # 1080p downscale from UHD BluRay must be recognized as 1080p, not 2160p
        q_1080_uhd = parse_release_quality(
            "Spider-Man Brand New Day 2026 UHD BluRay 1080p DD+Atmos 5 1 DoVi HDR10+ x265-SM737"
        )
        self.assertEqual(q_1080_uhd.resolution, "1080p")
        self.assertTrue(q_1080_uhd.dolby_vision)
        self.assertEqual(q_1080_uhd.hdr_label, "Dolby Vision + HDR10+")

        # DVDRip must never trigger a false Dolby Vision match
        q_dvd = parse_release_quality("Naughty but Nice 1939 1080p DVDRIP AVC AAC LC")
        self.assertFalse(q_dvd.dolby_vision)
        self.assertEqual(q_dvd.hdr_label, "SDR")

        # NFO MediaInfo Dolby Vision detection
        q_nfo = parse_release_quality(
            "Some.Film.2025.2160p.BluRay.REMUX.HEVC-GRP",
            nfo_text="HDR format : Dolby Vision, Version 1.0 / SMPTE ST 2086, HDR10 compatible",
        )
        self.assertTrue(q_nfo.dolby_vision)
        self.assertEqual(q_nfo.cache_key, "2160p_remux_dv")
        self.assertEqual(q_nfo.release_group, "GRP")
        self.assertFalse(q_nfo.is_favorite_group)

        # Favorite release group (CiNEPHiLES) detection from raw_text and cache separation
        q_cine = parse_release_quality(
            "Sharkys Machine 1981 2160p UHD Blu-ray Remux DV HDR HEVC DTS-HD MA 5 1-CiNEPHiLES"
        )
        self.assertEqual(q_cine.release_group, "CiNEPHiLES")
        self.assertTrue(q_cine.is_favorite_group)
        self.assertEqual(q_cine.cache_key, "2160p_remux_dv_fav_cinephiles")
        self.assertIn("FAVORITE RELEASE GROUP", q_cine.to_prompt_line() or "")

        # Explicit tracker group field overrides/supplements raw_text
        q_cine_explicit = parse_release_quality(
            "Some Film 1985 1080p BluRay REMUX AVC",
            group="cinephiles",
        )
        self.assertTrue(q_cine_explicit.is_favorite_group)
        self.assertEqual(q_cine_explicit.cache_key, "1080p_remux_sdr_fav_cinephiles")

        # Non-group suffixes and hyphenated titles are NOT misparsed as release groups
        self.assertIsNone(parse_release_quality("Spider-Man").release_group)
        self.assertIsNone(parse_release_quality("Movie 2025 1080p WEB-DL").release_group)
        self.assertIsNone(parse_release_quality("Movie 2025 2160p UHD Blu-ray").release_group)

    def test_dolby_vision_profile7_fel_mel_and_guards(self) -> None:
        from db import parse_release_quality

        # 1) Standard MediaInfo P7 (BL+EL+RPU without FEL/MEL) -> unconfirmed P7
        q_p7_unknown = parse_release_quality(
            "Movie.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-TRiToN",
            nfo_text="HDR format : Dolby Vision, Version 1.0, Profile 7.6, dvhe.07.06, BL+EL+RPU, Blu-ray compatible / SMPTE ST 2086, HDR10 compatible",
        )
        self.assertEqual(q_p7_unknown.dv_profile, "7")
        self.assertIsNone(q_p7_unknown.dv_el_type)
        self.assertEqual(q_p7_unknown.cache_key, "2160p_remux_dv_p7")
        self.assertIn("NFO does not specify FEL vs MEL", q_p7_unknown.to_prompt_line() or "")

        # 2) Explicit FEL via new MediaInfo RPU / uploader notes -> P7 FEL top tier
        q_p7_fel = parse_release_quality(
            "Movie.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-CiNEPHiLES",
            nfo_text="HDR format : Dolby Vision, Version 1.0, Profile 7.6, dvhe.07.06, BL+EL+RPU, FEL",
        )
        self.assertEqual(q_p7_fel.dv_profile, "7")
        self.assertEqual(q_p7_fel.dv_el_type, "FEL")
        self.assertEqual(q_p7_fel.dv_el_source, "explicit")
        self.assertEqual(q_p7_fel.cache_key, "2160p_remux_dv_p7_fel_fav_cinephiles")
        self.assertIn("TOP PREFERRED TIER", q_p7_fel.to_prompt_line() or "")

        # 3) Explicit [MEL] in CiNEPHiLES description -> P7 MEL
        q_p7_mel = parse_release_quality(
            "Movie.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-CiNEPHiLES",
            description="Used superior [MEL] video from Arrow UK UHD Blu-ray",
        )
        self.assertEqual(q_p7_mel.dv_profile, "7")
        self.assertEqual(q_p7_mel.dv_el_type, "MEL")
        self.assertEqual(q_p7_mel.cache_key, "2160p_remux_dv_p7_mel_fav_cinephiles")

        # 4) BDInfo 1080p EL bitrate heuristic (>=3000 kbps = FEL, <3000 kbps = MEL)
        q_bd_fel = parse_release_quality(
            "Movie.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-GRP",
            nfo_text=(
                "MPEG-H HEVC Video     55123 kbps   2160p / 23.976 fps / 16:9\n"
                "* MPEG-H HEVC Video    6842 kbps   1080p / 23.976 fps / 16:9"
            ),
        )
        self.assertEqual(q_bd_fel.dv_profile, "7")
        self.assertEqual(q_bd_fel.dv_el_type, "FEL")
        self.assertEqual(q_bd_fel.dv_el_source, "bdinfo_bitrate")
        self.assertEqual(q_bd_fel.cache_key, "2160p_remux_dv_p7_fel")

        q_bd_mel = parse_release_quality(
            "Movie.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-GRP",
            nfo_text="* MPEG-H HEVC Video    1.25 Mbps   1080p / 23.976 fps / 16:9",
        )
        self.assertEqual(q_bd_mel.dv_profile, "7")
        self.assertEqual(q_bd_mel.dv_el_type, "MEL")
        self.assertEqual(q_bd_mel.dv_el_source, "bdinfo_bitrate")

        # 5) Profile 8 single-layer + negated FEL guard ("converted from FEL to Profile 8.1")
        q_p8 = parse_release_quality(
            "Movie.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-GRP",
            nfo_text="HDR format : Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU",
            description="Converted from FEL to Profile 8.1 for broad compatibility.",
        )
        self.assertEqual(q_p8.dv_profile, "8")
        self.assertIsNone(q_p8.dv_el_type)
        self.assertEqual(q_p8.cache_key, "2160p_remux_dv_p8")

        # 6) False-positive guards: "Mel Gibson" prose and ambiguous "FEL/MEL" or dual mention
        q_prose = parse_release_quality(
            "Ransom.1996.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-GRP",
            description="Mel Gibson stars as a wealthy airline executive whose son is kidnapped.",
        )
        self.assertIsNone(q_prose.dv_el_type)

        q_both = parse_release_quality(
            "Movie.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-GRP",
            description="Comparison: US disc [FEL] vs UK disc [MEL] (FEL/MEL noted).",
        )
        self.assertIsNone(q_both.dv_el_type)

    def test_evaluate_endpoint_wires_tmdb_grounding_and_cache(self) -> None:
        import main

        tmdb_hit = TmdbMovie(
            tmdb_id=837335,
            canonical_title="La Chimera",
            original_title="La chimera",
            year=2023,
            imdb_id="tt14561712",
            runtime_minutes=133,
            director="Alice Rohrwacher",
            genres=["Drama", "Comedy", "Fantasy", "Adventure"],
        )
        gemini_raw = _sample_payload(
            title="La Chimera",
            original_title=None,
            year=2023,
            director="Alice Rohrwacher",
            imdb_id=None,
            tmdb_id=None,
            runtime_minutes=None,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = TriageDB(Path(tmpdir) / "triage.db")
            test_db.init_db()
            from tmdb_client import TmdbLookupResult

            mock_tmdb = TmdbClient(api_key="dummy-key")
            mock_tmdb.lookup_with_diagnostics = AsyncMock(  # type: ignore[method-assign]
                return_value=TmdbLookupResult(
                    movie=tmdb_hit,
                    tracker_conflict=None,
                    trusted_for_cache=True,
                )
            )
            mock_tmdb.verify_identification = AsyncMock(return_value=None)  # type: ignore[method-assign]
            mock_gemini_eval = AsyncMock(return_value=gemini_raw)

            with (
                patch.object(main, "db", test_db),
                patch.object(main, "tmdb_client", mock_tmdb),
                patch.object(main.gemini_client, "evaluate", mock_gemini_eval),
            ):
                # 1) Uncached 1080p Remux evaluation gets TMDB grounding + release quality context
                resp1 = asyncio.run(
                    main.evaluate(
                        EvaluateRequest(raw_text="La.Chimera.2023.1080p.BluRay.REMUX.AVC-GROUP")
                    )
                )
                self.assertFalse(resp1.cached)
                self.assertEqual(resp1.identification.tmdb_id, 837335)
                self.assertEqual(resp1.identification.imdb_id, "tt14561712")
                self.assertEqual(resp1.identification.runtime_minutes, 133)
                self.assertEqual(resp1.identification.original_title, "La chimera")
                self.assertEqual(mock_gemini_eval.await_count, 1)
                passed_ctx = mock_gemini_eval.await_args.kwargs["tmdb_context"]
                passed_q = mock_gemini_eval.await_args.kwargs["release_quality_context"]
                self.assertIn("TMDB ID: 837335", passed_ctx)
                self.assertIn("Remux-1080p", passed_q)

                # 2) Same quality tier alias hits cache via TMDB -> IMDb ID (no 2nd Gemini call)
                resp2 = asyncio.run(
                    main.evaluate(
                        EvaluateRequest(raw_text="The.Chimera.2023.1080p.BluRay.REMUX-OTHER")
                    )
                )
                self.assertTrue(resp2.cached)
                self.assertEqual(resp2.identification.tmdb_id, 837335)
                self.assertEqual(mock_gemini_eval.await_count, 1)

                # 3) Upgraded 2160p DV Remux misses 1080p cache and passes 2160p DV context to Gemini
                resp3 = asyncio.run(
                    main.evaluate(
                        EvaluateRequest(raw_text="La.Chimera.2023.2160p.UHD.BluRay.REMUX.DV.HDR-TRiToN")
                    )
                )
                self.assertFalse(resp3.cached)
                self.assertEqual(mock_gemini_eval.await_count, 2)
                passed_q3 = mock_gemini_eval.await_args.kwargs["release_quality_context"]
                self.assertIn("Remux-2160p", passed_q3)
                self.assertIn("Dolby Vision", passed_q3)

    def test_tracker_info_conflict_rejected_and_falls_back_cleanly(self) -> None:
        from schemas import TorrentInfo
        from tmdb_client import (
            check_tracker_movie_consistency,
            format_torrent_info_for_prompt,
            sanitize_tracker_text,
        )

        # Wrong-year remake linked by tracker must be flagged as conflict
        wrong_remake = TmdbMovie(
            tmdb_id=999,
            canonical_title="Silent Night, Deadly Night",
            original_title="Silent Night, Deadly Night",
            year=1984,
            imdb_id="tt0088117",
            runtime_minutes=79,
            original_language="en",
        )
        conflict = check_tracker_movie_consistency(
            wrong_remake,
            query_title="Silent Night Deadly Night",
            query_year=2025,
            nfo_runtime=97,
        )
        self.assertIsNotNone(conflict)
        assert conflict is not None
        self.assertIn("year mismatch", conflict)
        self.assertIn("runtime mismatch", conflict)

        # Non-Latin romanized title corroborated by matching runtime & language is accepted
        jp_movie = TmdbMovie(
            tmdb_id=372058,
            canonical_title="Your Name.",
            original_title="君の名は。",
            year=2016,
            imdb_id="tt5311514",
            runtime_minutes=106,
            original_language="ja",
        )
        self.assertIsNone(
            check_tracker_movie_consistency(
                jp_movie,
                query_title="Kimi no Na wa",
                query_year=2016,
                nfo_runtime=106,
                audio_languages=["Japanese"],
            )
        )

        # Secret redaction scrubs URLs, passkeys, cookies, and long tokens
        dirty = (
            "Great film! See https://www.torrentleech.org/torrent/12345 "
            "passkey=supersecret123 cookie=abc magnet:?xt=urn:btih:deadbeef "
            + "X" * 60
        )
        clean = sanitize_tracker_text(dirty)
        self.assertNotIn("torrentleech.org", clean)
        self.assertNotIn("supersecret123", clean)
        self.assertNotIn("magnet:", clean)
        self.assertNotIn("X" * 48, clean)

        # Conflict warning banner appears in prompt block
        prompt_block = format_torrent_info_for_prompt(
            TorrentInfo(
                indexer="TorrentLeech",
                imdb_id="tt0088117",
                nfo_text="General\nDuration : 1 h 37 min\nLanguage : English",
                description="Uploaded from tracker",
            ),
            conflict_warning=conflict,
        )
        self.assertIsNotNone(prompt_block)
        assert prompt_block is not None
        self.assertIn("CONFLICT WARNING:", prompt_block)
        self.assertIn("UNTRUSTED", prompt_block)
        self.assertIn("MediaInfo file duration: 97 minutes", prompt_block)

    def test_tmdb_runtime_tiebreaker_and_conflict_fallback_in_client(self) -> None:
        from schemas import TorrentInfo

        client = TmdbClient(api_key="0123456789abcdef0123456789abcdef")

        # Simulate tracker linking wrong 1984 movie (id=10) when release is 2025 movie (id=20),
        # while two 2025 candidates exist (id=20 at 113m Cantonese, id=21 at 88m English).
        fake_search = [
            {"id": 21, "title": "The Furious", "original_title": "The Furious", "release_date": "2025-04-01", "popularity": 25.0},
            {"id": 20, "title": "The Furious", "original_title": "火遮眼", "release_date": "2025-09-06", "popularity": 18.0},
        ]
        fake_details = {
            10: TmdbMovie(tmdb_id=10, canonical_title="The Furious", original_title="The Furious", year=1984, imdb_id="tt0000010", runtime_minutes=80, original_language="en"),
            20: TmdbMovie(tmdb_id=20, canonical_title="The Furious", original_title="火遮眼", year=2025, imdb_id="tt33311069", runtime_minutes=113, original_language="zh"),
            21: TmdbMovie(tmdb_id=21, canonical_title="The Furious", original_title="The Furious", year=2025, imdb_id="tt0000021", runtime_minutes=88, original_language="en"),
        }

        req = EvaluateRequest(
            raw_text="The.Furious.2025.2160p.UHD.BluRay.REMUX-GRP",
            torrent_info=TorrentInfo(
                indexer="TorrentLeech",
                imdb_id="tt0000010",
                runtime_minutes=113,
                audio_languages=["Cantonese"],
            ),
        )

        with (
            patch.object(client, "_find_tmdb_id_by_imdb", return_value=10),
            patch.object(client, "_search_results", return_value=fake_search),
            patch.object(client, "_fetch_movie_details", side_effect=lambda tid: fake_details[tid]),
        ):
            res = asyncio.run(client.lookup_with_diagnostics(req))
            self.assertIsNotNone(res.tracker_conflict)
            self.assertFalse(res.trusted_for_cache)
            self.assertIsNotNone(res.movie)
            assert res.movie is not None
            # MediaInfo 113m + Cantonese broke the tie in favor of id=20 over more popular id=21!
            self.assertEqual(res.movie.tmdb_id, 20)
            self.assertEqual(res.movie.imdb_id, "tt33311069")

    def test_feedback_watch_skip_normalization_and_dedup(self) -> None:
        from pydantic import ValidationError
        import main
        from schemas import FeedbackRequest

        # Lowercase watch / skip normalize cleanly to WATCH / SKIP
        req_watch = FeedbackRequest(
            imdb_id="tt0432283",
            tmdb_id=10315,
            title="Fantastic Mr. Fox",
            decision="watch",  # type: ignore[arg-type]
            note="Grabbed via Telegram",
        )
        self.assertEqual(req_watch.decision, "WATCH")

        req_skip = FeedbackRequest(
            imdb_id="tt0105104",
            title="Passenger 57",
            decision="skip",  # type: ignore[arg-type]
        )
        self.assertEqual(req_skip.decision, "SKIP")

        # Non watch/skip values (including MAYBE) are rejected
        with self.assertRaises(ValidationError):
            FeedbackRequest(title="Some Film", decision="maybe")  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = TriageDB(Path(tmpdir) / "triage.db")
            test_db.init_db()
            with patch.object(main, "db", test_db):
                r1 = asyncio.run(main.submit_feedback(req_watch))
                self.assertEqual(r1.status, "saved")
                self.assertEqual(r1.decision, "WATCH")
                self.assertEqual(r1.total_feedback, 1)

                r2 = asyncio.run(main.submit_feedback(req_skip))
                self.assertEqual(r2.decision, "SKIP")
                self.assertEqual(r2.total_feedback, 2)

                # Re-submitting for same IMDb ID updates decision without duplicating row
                r3 = asyncio.run(
                    main.submit_feedback(
                        FeedbackRequest(
                            imdb_id="tt0432283",
                            title="Fantastic Mr. Fox",
                            decision="skip",  # type: ignore[arg-type]
                            note="Changed mind",
                        )
                    )
                )
                self.assertEqual(r3.decision, "SKIP")
                self.assertEqual(r3.total_feedback, 2)

                items = asyncio.run(main.list_feedback(limit=10))
                self.assertEqual(len(items), 2)
                self.assertEqual(items[0].imdb_id, "tt0432283")
                self.assertEqual(items[0].decision, "SKIP")
                self.assertEqual(items[0].note, "Changed mind")

    def test_internal_token_auth_enforcement_and_health_bypass(self) -> None:
        from fastapi.testclient import TestClient
        import main
        from tmdb_client import TmdbLookupResult

        gemini_raw = _sample_payload(imdb_id="tt14561712", tmdb_id=837335)
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = TriageDB(Path(tmpdir) / "triage.db")
            test_db.init_db()
            mock_tmdb = TmdbClient(api_key="")
            mock_tmdb.lookup_with_diagnostics = AsyncMock(  # type: ignore[method-assign]
                return_value=TmdbLookupResult()
            )
            mock_gemini_eval = AsyncMock(return_value=gemini_raw)

            with (
                patch.object(main, "db", test_db),
                patch.object(main, "tmdb_client", mock_tmdb),
                patch.object(main.gemini_client, "evaluate", mock_gemini_eval),
                patch.object(main, "INTERNAL_API_SECRET", "s3cr3t-loopback-token"),
                TestClient(main.app) as client,
            ):
                # /health stays unauthenticated and reports auth_enabled=True
                health = client.get("/health")
                self.assertEqual(health.status_code, 200)
                self.assertTrue(health.json()["auth_enabled"])

                # Missing or wrong X-Internal-Token returns 401 across protected endpoints
                for method, path, body in (
                    ("POST", "/evaluate", {"raw_text": "La.Chimera.2023.1080p.REMUX-GRP"}),
                    ("POST", "/feedback", {"title": "La Chimera", "decision": "watch"}),
                    ("GET", "/feedback", None),
                    ("GET", "/recent", None),
                ):
                    r_missing = client.request(method, path, json=body)
                    self.assertEqual(r_missing.status_code, 401)
                    self.assertEqual(r_missing.json(), {"detail": "Unauthorized"})

                    r_wrong = client.request(
                        method,
                        path,
                        json=body,
                        headers={"X-Internal-Token": "wrong-token"},
                    )
                    self.assertEqual(r_wrong.status_code, 401)

                # Valid X-Internal-Token succeeds
                headers = {"X-Internal-Token": "s3cr3t-loopback-token"}
                r_eval = client.post(
                    "/evaluate",
                    json={"raw_text": "La.Chimera.2023.1080p.REMUX-GRP"},
                    headers=headers,
                )
                self.assertEqual(r_eval.status_code, 200)
                self.assertFalse(r_eval.json()["cached"])

                r_fb = client.post(
                    "/feedback",
                    json={"title": "La Chimera", "imdb_id": "tt14561712", "decision": "watch"},
                    headers=headers,
                )
                self.assertEqual(r_fb.status_code, 200)
                self.assertEqual(r_fb.json()["status"], "saved")
                self.assertEqual(client.get("/feedback", headers=headers).status_code, 200)
                self.assertEqual(client.get("/recent", headers=headers).status_code, 200)

            # Issue #13 §5b: Fail closed (503) when INTERNAL_API_SECRET is empty, tested independently
            with (
                patch.object(main, "db", test_db),
                patch.object(main, "INTERNAL_API_SECRET", ""),
                TestClient(main.app) as client,
            ):
                health_unconf = client.get("/health")
                self.assertEqual(health_unconf.status_code, 200)
                self.assertFalse(health_unconf.json()["auth_enabled"])
                self.assertEqual(client.get("/recent").status_code, 503)
                self.assertEqual(client.get("/recent", headers={"X-Internal-Token": "anything"}).status_code, 503)
                self.assertEqual(client.get("/feedback").status_code, 503)
                self.assertEqual(
                    client.post(
                        "/feedback",
                        json={"title": "La Chimera", "imdb_id": "tt14561712", "decision": "watch"},
                    ).status_code,
                    503,
                )
                self.assertEqual(
                    client.post("/evaluate", json={"raw_text": "La.Chimera.2023.1080p.REMUX-GRP"}).status_code,
                    503,
                )

    def test_compute_evidence_key_sensitivity_and_empty_parity(self) -> None:
        from schemas import TorrentInfo
        from tmdb_client import compute_evidence_key

        # Plain release string and all-empty TorrentInfo both map to ""
        self.assertEqual(compute_evidence_key(EvaluateRequest(raw_text="La.Chimera.2023")), "")
        self.assertEqual(
            compute_evidence_key(EvaluateRequest(raw_text="La.Chimera.2023", torrent_info=TorrentInfo())),
            "",
        )

        base_req = EvaluateRequest(
            raw_text="The.Furious.2025.2160p.UHD.BluRay.REMUX-GRP",
            torrent_info=TorrentInfo(
                indexer="TorrentLeech",
                imdb_id="tt33311069",
                runtime_minutes=113,
                audio_languages=["Cantonese", "English"],
                freeleech=False,
                size_gb=64.25,
                description="Clean disc description",
            ),
        )
        base_key = compute_evidence_key(base_req)
        self.assertTrue(base_key.startswith("ev1:"))

        # Order-insensitive lists and case-insensitive tokens produce identical digest
        same_req = EvaluateRequest(
            raw_text="The.Furious.2025.2160p.UHD.BluRay.REMUX-GRP",
            torrent_info=TorrentInfo(
                indexer="torrentleech",
                imdb_id="TT33311069",
                runtime_minutes=113,
                audio_languages=["english", "cantonese"],
                freeleech=False,
                size_gb=64.2501,
                description="Clean disc description",
            ),
        )
        self.assertEqual(compute_evidence_key(same_req), base_key)

        # Changing freeleech False -> True, imdb_id, runtime, or description changes digest
        self.assertNotEqual(
            compute_evidence_key(
                base_req.model_copy(
                    update={"torrent_info": base_req.torrent_info.model_copy(update={"freeleech": True})}  # type: ignore[union-attr]
                )
            ),
            base_key,
        )
        self.assertNotEqual(
            compute_evidence_key(
                base_req.model_copy(
                    update={"torrent_info": base_req.torrent_info.model_copy(update={"freeleech": None})}  # type: ignore[union-attr]
                )
            ),
            base_key,
        )
        self.assertNotEqual(
            compute_evidence_key(
                base_req.model_copy(
                    update={"torrent_info": base_req.torrent_info.model_copy(update={"runtime_minutes": 95})}  # type: ignore[union-attr]
                )
            ),
            base_key,
        )
        self.assertNotEqual(
            compute_evidence_key(
                base_req.model_copy(
                    update={"torrent_info": base_req.torrent_info.model_copy(update={"imdb_id": "tt0000010"})}  # type: ignore[union-attr]
                )
            ),
            base_key,
        )
        self.assertNotEqual(
            compute_evidence_key(EvaluateRequest(raw_text="Film", hint_year=1999)),
            compute_evidence_key(EvaluateRequest(raw_text="Film", hint_year=2024)),
        )

        # Secret URLs in description are scrubbed prior to hashing
        k_url_a = compute_evidence_key(
            EvaluateRequest(
                raw_text="Film.2024",
                torrent_info=TorrentInfo(description="See https://tracker.example/dl?passkey=aaa"),
            )
        )
        k_url_b = compute_evidence_key(
            EvaluateRequest(
                raw_text="Film.2024",
                torrent_info=TorrentInfo(description="See https://tracker.example/dl?passkey=bbb"),
            )
        )
        self.assertEqual(k_url_a, k_url_b)

    def test_evaluate_cache_scoped_by_evidence_key_and_blocks_poisoned_imdb(self) -> None:
        import main
        from schemas import TorrentInfo
        from tmdb_client import TmdbLookupResult

        movie_2025 = _sample_payload(
            title="Silent Night, Deadly Night",
            year=2025,
            director="Mike P. Nelson",
            imdb_id="tt33000025",
            tmdb_id=202599,
            runtime_minutes=97,
        )
        movie_1984 = _sample_payload(
            title="Silent Night, Deadly Night",
            year=1984,
            director="Charles E. Sellier Jr.",
            imdb_id="tt0088117",
            tmdb_id=198499,
            runtime_minutes=79,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = TriageDB(Path(tmpdir) / "triage.db")
            test_db.init_db()
            # Pre-seed 1984 original in cache under plain evidence_key=""
            test_db.save_evaluation(
                "Silent.Night.Deadly.Night.1984.2160p.UHD.BluRay.REMUX-GRP",
                movie_1984.model_dump(),
            )

            mock_tmdb = TmdbClient(api_key="dummy-key")
            mock_tmdb.lookup_with_diagnostics = AsyncMock(  # type: ignore[method-assign]
                return_value=TmdbLookupResult(
                    movie=None,
                    tracker_conflict="year mismatch",
                    trusted_for_cache=False,
                )
            )
            mock_tmdb.verify_identification = AsyncMock(return_value=None)  # type: ignore[method-assign]
            mock_gemini_eval = AsyncMock(return_value=movie_2025)

            with (
                patch.object(main, "db", test_db),
                patch.object(main, "tmdb_client", mock_tmdb),
                patch.object(main.gemini_client, "evaluate", mock_gemini_eval),
            ):
                # 1) Plain 2025 release with poisoned 1984 IMDb in extra_context MUST NOT hit 1984 cache
                # and an unresolved tracker conflict with no release match MUST NOT be marked compatible or cached
                r1 = asyncio.run(
                    main.evaluate(
                        EvaluateRequest(
                            raw_text="Silent.Night.Deadly.Night.2025.2160p.UHD.BluRay.REMUX-GRP",
                            extra_context="tt0088117",
                        )
                    )
                )
                self.assertFalse(r1.cached)
                self.assertEqual(r1.identification.year, 2025)
                assert r1.catalogue_identification is not None
                self.assertFalse(r1.catalogue_identification.compatible)
                self.assertEqual(mock_gemini_eval.await_count, 1)

                # Resolve 2025 movie cleanly for evidence_key scoping assertions (steps 2-4)
                movie_2025_tmdb = TmdbMovie(
                    tmdb_id=202599,
                    canonical_title="Silent Night, Deadly Night",
                    original_title="Silent Night, Deadly Night",
                    year=2025,
                    imdb_id="tt33000025",
                    director="Mike P. Nelson",
                    runtime_minutes=97,
                )
                mock_tmdb.lookup_with_diagnostics = AsyncMock(  # type: ignore[method-assign]
                    return_value=TmdbLookupResult(
                        movie=movie_2025_tmdb,
                        tracker_conflict=None,
                        trusted_for_cache=True,
                    )
                )

                # 2) Same 2025 release with new MediaInfo/tracker evidence MUST miss cache
                tinfo_a = TorrentInfo(
                    indexer="TorrentLeech",
                    imdb_id="tt33000025",
                    runtime_minutes=97,
                    freeleech=False,
                )
                r2 = asyncio.run(
                    main.evaluate(
                        EvaluateRequest(
                            raw_text="Silent.Night.Deadly.Night.2025.2160p.UHD.BluRay.REMUX-GRP",
                            torrent_info=tinfo_a,
                        )
                    )
                )
                self.assertFalse(r2.cached)
                self.assertEqual(mock_gemini_eval.await_count, 2)

                # 3) Identical evidence hits cache
                r3 = asyncio.run(
                    main.evaluate(
                        EvaluateRequest(
                            raw_text="Silent.Night.Deadly.Night.2025.2160p.UHD.BluRay.REMUX-GRP",
                            torrent_info=tinfo_a,
                        )
                    )
                )
                self.assertTrue(r3.cached)
                self.assertEqual(mock_gemini_eval.await_count, 2)

                # 4) Changed evidence (freeleech False -> True) misses cache
                r4 = asyncio.run(
                    main.evaluate(
                        EvaluateRequest(
                            raw_text="Silent.Night.Deadly.Night.2025.2160p.UHD.BluRay.REMUX-GRP",
                            torrent_info=tinfo_a.model_copy(update={"freeleech": True}),
                        )
                    )
                )
                self.assertFalse(r4.cached)
                self.assertEqual(mock_gemini_eval.await_count, 3)

    def test_db_legacy_migration_private_permissions_and_symlink_rejection(self) -> None:
        import json
        import os
        import sqlite3
        import stat

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "private_data"
            data_dir.mkdir(mode=0o755)
            db_file = data_dir / "triage.db"

            # Create a legacy evaluations table missing quality_key and evidence_key
            legacy_payload = _sample_payload(
                title="Silent Night, Deadly Night",
                year=1984,
                imdb_id="tt0088117",
            ).model_dump()
            with sqlite3.connect(str(db_file)) as conn:
                conn.execute(
                    """
                    CREATE TABLE evaluations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        raw_text_normalized TEXT NOT NULL,
                        release_key TEXT,
                        raw_text_original TEXT NOT NULL,
                        imdb_id TEXT,
                        canonical_title TEXT NOT NULL,
                        year INTEGER NOT NULL,
                        verdict TEXT NOT NULL,
                        response_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO evaluations (
                        raw_text_normalized, release_key, raw_text_original,
                        imdb_id, canonical_title, year, verdict, response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "silent night deadly night 1984",
                        "silent night deadly night 1984",
                        "Silent Night Deadly Night 1984",
                        "tt0088117",
                        "Silent Night, Deadly Night",
                        1984,
                        "WATCH",
                        json.dumps(legacy_payload),
                        "2099-01-01T00:00:00+00:00",
                    ),
                )
                # Also seed an unversioned legacy row with an out-of-range rating (must miss without ValidationError)
                bad_legacy = json.loads(json.dumps(legacy_payload))
                bad_legacy["ratings"]["imdb"] = 11.4
                bad_legacy["ratings"]["sources"] = {}
                conn.execute(
                    """
                    INSERT INTO evaluations (
                        raw_text_normalized, release_key, raw_text_original,
                        imdb_id, canonical_title, year, verdict, response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy bad score 1984",
                        "legacy bad score 1984",
                        "Legacy Bad Score 1984",
                        "tt0088118",
                        "Legacy Bad Score",
                        1984,
                        "WATCH",
                        json.dumps(bad_legacy),
                        "2099-01-01T00:00:00+00:00",
                    ),
                )
                # Seed a pre-upgrade feedback table row (no tmdb_id column) to verify feedback preservation
                conn.execute(
                    """
                    CREATE TABLE feedback (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        imdb_id TEXT,
                        title TEXT NOT NULL,
                        decision TEXT NOT NULL,
                        note TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO feedback (imdb_id, title, decision, note, created_at)
                    VALUES ('tt0088117', 'Silent Night, Deadly Night', 'WATCH', 'watched', '2026-11-25T00:00:00+00:00')
                    """
                )

            db = TriageDB(db_file)
            db.init_db()

            # Directory is tightened to 0700 and DB file to 0600
            self.assertEqual(stat.S_IMODE(data_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(db_file.stat().st_mode), 0o600)

            # PR #18 Gap 1: Unversioned pre-upgrade evaluation rows MUST cleanly miss cache (never serving
            # unvalidated/unsourced ratings or crashing on out-of-range scores), while feedback is preserved!
            self.assertIsNone(db.get_cached_evaluation("Silent Night Deadly Night 1984"))
            self.assertIsNone(db.get_cached_evaluation("Legacy Bad Score 1984"))
            preserved_fb = db.get_recent_feedback(10)
            self.assertEqual(len(preserved_fb), 1)
            self.assertEqual(preserved_fb[0]["imdb_id"], "tt0088117")
            self.assertEqual(preserved_fb[0]["decision"], "WATCH")

            # Newly saved v2 row hits cache cleanly and stays blocked across remake years
            db.save_evaluation("Silent Night Deadly Night 1984", legacy_payload)
            hit = db.get_cached_evaluation("Silent Night Deadly Night 1984")
            self.assertIsNotNone(hit)
            cross_year_miss = db.get_cached_evaluation(
                "Silent.Night.Deadly.Night.2025.2160p.UHD.BluRay.REMUX-GRP",
                extra_context="tt0088117",
                quality_key="",
                evidence_key="",
            )
            self.assertIsNone(cross_year_miss)

            # Symlinked DB path is rejected
            link_path = data_dir / "link.db"
            os.symlink(db_file, link_path)
            with self.assertRaises(RuntimeError):
                TriageDB(link_path).init_db()

    def test_secret_redaction_in_prompts_and_upstream_errors(self) -> None:
        from gemini_client import GeminiClientError, GeminiTriageClient, build_system_instruction

        req = EvaluateRequest(
            raw_text="La.Chimera.2023.1080p.REMUX-GRP",
            extra_context="tt14561712 https://tracker.example/dl?passkey=secret999 api_key=abc123",
        )
        prompt = build_user_prompt(req)
        self.assertIn("tt14561712", prompt)
        self.assertNotIn("secret999", prompt)
        self.assertNotIn("abc123", prompt)
        self.assertNotIn("tracker.example", prompt)

        sys_instr = build_system_instruction(
            "Taste profile",
            [{"decision": "WATCH", "title": "La Chimera", "note": "cookie=topsecret https://evil.test/x"}],
        )
        self.assertNotIn("topsecret", sys_instr)
        self.assertNotIn("evil.test", sys_instr)

        # GeminiClientError redacts API key / URL from upstream exception messages
        client = GeminiTriageClient(api_key="AIzaDummyKeyForTest")
        with patch.object(
            client,
            "_call_sync",
            side_effect=RuntimeError(
                "Request to https://generativelanguage.googleapis.com/v1?key=AIzaSuperSecretKey failed"
            ),
        ):
            with self.assertRaises(GeminiClientError) as ctx:
                asyncio.run(client.evaluate(req, []))
            self.assertNotIn("AIzaSuperSecretKey", str(ctx.exception))
            self.assertNotIn("googleapis.com", str(ctx.exception))

        # TMDB client warning logs redact v3 api_key URLs
        tmdb = TmdbClient(api_key="0123456789abcdef0123456789abcdef")
        with (
            patch.object(
                tmdb,
                "_lookup_with_diagnostics_sync",
                side_effect=urllib.error.URLError(
                    "https://api.themoviedb.org/3/movie/1?api_key=0123456789abcdef0123456789abcdef"
                ),
            ),
            self.assertLogs("tmdb_client", level="WARNING") as logs,
        ):
            res = asyncio.run(tmdb.lookup_with_diagnostics(req))
            self.assertIsNone(res.movie)
            combined_logs = "\n".join(logs.output)
            self.assertNotIn("0123456789abcdef0123456789abcdef", combined_logs)

    def test_direct_imdb_conflict_rejected_while_bare_imdb_resolves(self) -> None:
        client = TmdbClient(api_key="0123456789abcdef0123456789abcdef")
        movie_1984 = TmdbMovie(
            tmdb_id=10,
            canonical_title="Silent Night, Deadly Night",
            original_title="Silent Night, Deadly Night",
            year=1984,
            imdb_id="tt0088117",
            runtime_minutes=79,
            original_language="en",
        )
        movie_2025 = TmdbMovie(
            tmdb_id=20,
            canonical_title="Silent Night, Deadly Night",
            original_title="Silent Night, Deadly Night",
            year=2025,
            imdb_id="tt33000025",
            runtime_minutes=97,
            original_language="en",
        )

        with (
            patch.object(client, "_find_tmdb_id_by_imdb", return_value=10),
            patch.object(
                client,
                "_search_results",
                return_value=[
                    {
                        "id": 20,
                        "title": "Silent Night, Deadly Night",
                        "original_title": "Silent Night, Deadly Night",
                        "release_date": "2025-12-01",
                        "popularity": 20.0,
                    }
                ],
            ),
            patch.object(
                client,
                "_fetch_movie_details",
                side_effect=lambda tid: movie_1984 if tid == 10 else movie_2025,
            ),
        ):
            # Conflicting 1984 IMDb in extra_context for a 2025 release is flagged & falls back to 2025
            res_conflict = asyncio.run(
                client.lookup_with_diagnostics(
                    EvaluateRequest(
                        raw_text="Silent.Night.Deadly.Night.2025.2160p.UHD.BluRay.REMUX-GRP",
                        extra_context="tt0088117",
                    )
                )
            )
            self.assertIsNotNone(res_conflict.tracker_conflict)
            self.assertFalse(res_conflict.trusted_for_cache)
            self.assertIsNotNone(res_conflict.movie)
            assert res_conflict.movie is not None
            self.assertEqual(res_conflict.movie.tmdb_id, 20)

            # Bare IMDb lookup with no conflicting title/year still resolves directly
            res_bare = asyncio.run(
                client.lookup_with_diagnostics(EvaluateRequest(raw_text="tt0088117"))
            )
            self.assertIsNone(res_bare.tracker_conflict)
            self.assertTrue(res_bare.trusted_for_cache)
            self.assertIsNotNone(res_bare.movie)
            assert res_bare.movie is not None
            self.assertEqual(res_bare.movie.tmdb_id, 10)

    def test_issue_13_3a_conflicting_tracker_imdb_and_tmdb_both_resolved_and_not_trusted(self) -> None:
        """Issue #13 §3a: When tracker supplies BOTH tmdb_id and imdb_id, resolve and compare BOTH."""
        from schemas import TorrentInfo

        client = TmdbClient(api_key="0123456789abcdef0123456789abcdef")
        movie_a = TmdbMovie(
            tmdb_id=1001,
            canonical_title="Harbor Line",
            original_title="Harbor Line",
            year=2024,
            imdb_id="tt1111111",
            runtime_minutes=112,
            director="Clara Lindholm",
        )
        movie_b = TmdbMovie(
            tmdb_id=2002,
            canonical_title="Harbor Line",
            original_title="Harbor Line",
            year=2024,
            imdb_id="tt2222222",
            runtime_minutes=112,
            director="Tomasz Nowak",
        )

        imdb_lookup_calls: list[str] = []

        def _find_by_imdb(imdb_id: str) -> int | None:
            imdb_lookup_calls.append(imdb_id)
            return {"tt1111111": 1001, "tt2222222": 2002}.get(imdb_id.lower())

        def _fetch(tid: int) -> TmdbMovie:
            return {1001: movie_a, 2002: movie_b}[tid]

        req_conflict = EvaluateRequest(
            raw_text="Harbor.Line.2024.2160p.UHD.BluRay.REMUX-GROUP",
            torrent_info=TorrentInfo(
                indexer="TorrentLeech",
                tmdb_id=1001,
                imdb_id="tt2222222",
            ),
        )
        with (
            patch.object(client, "_find_tmdb_id_by_imdb", side_effect=_find_by_imdb),
            patch.object(client, "_fetch_movie_details", side_effect=_fetch),
            patch.object(client, "_search_results", return_value=[]),
        ):
            res = asyncio.run(client.lookup_with_diagnostics(req_conflict))
            self.assertIn("tt2222222", imdb_lookup_calls)
            self.assertIsNotNone(res.tracker_conflict)
            assert res.tracker_conflict is not None
            self.assertIn("tt1111111", res.tracker_conflict)
            self.assertIn("tt2222222", res.tracker_conflict)
            self.assertFalse(res.trusted_for_cache)
            self.assertIsNone(res.movie)

        # Control: agreeing tracker tmdb_id + imdb_id pair resolves cleanly as trusted_for_cache=True
        imdb_lookup_calls.clear()
        req_ok = EvaluateRequest(
            raw_text="Harbor.Line.2024.2160p.UHD.BluRay.REMUX-GROUP",
            torrent_info=TorrentInfo(
                indexer="TorrentLeech",
                tmdb_id=1001,
                imdb_id="tt1111111",
            ),
        )
        with (
            patch.object(client, "_find_tmdb_id_by_imdb", side_effect=_find_by_imdb),
            patch.object(client, "_fetch_movie_details", side_effect=_fetch),
        ):
            res_ok = asyncio.run(client.lookup_with_diagnostics(req_ok))
            self.assertIn("tt1111111", imdb_lookup_calls)
            self.assertIsNone(res_ok.tracker_conflict)
            self.assertTrue(res_ok.trusted_for_cache)
            assert res_ok.movie is not None
            self.assertEqual(res_ok.movie.tmdb_id, 1001)

    def test_issue_13_3b_shared_director_first_name_and_contradictory_ids_rejected_and_separated(self) -> None:
        """Issue #13 §3b: Same-year films sharing director first name or conflicting TMDb IDs never merge."""
        import main
        from tmdb_client import TmdbLookupResult, is_tmdb_compatible

        film_a_payload = _sample_payload(
            title="Paper Harbor",
            year=2024,
            director="David Mercer",
            imdb_id=None,
            tmdb_id=50101,
        )
        film_b_tmdb = TmdbMovie(
            tmdb_id=50202,
            canonical_title="Silent Valley",
            original_title="Silent Valley",
            year=2024,
            imdb_id="tt5020200",
            runtime_minutes=104,
            director="David Kowalski",
        )

        # 1) Contradictory TMDB ID + shared first name ONLY -> strictly incompatible
        self.assertFalse(is_tmdb_compatible(film_a_payload.identification, film_b_tmdb))

        # 2) Even if model omitted tmdb_id, shared first name ONLY and different title -> incompatible
        film_a_no_id = _sample_payload(
            title="Paper Harbor",
            year=2024,
            director="David Mercer",
            imdb_id=None,
            tmdb_id=None,
        )
        self.assertFalse(is_tmdb_compatible(film_a_no_id.identification, film_b_tmdb))

        # 3) Same title + same year + directors sharing only first name -> incompatible
        same_title_other_dir = TmdbMovie(
            tmdb_id=50203,
            canonical_title="Paper Harbor",
            original_title="Paper Harbor",
            year=2024,
            imdb_id="tt5020300",
            runtime_minutes=104,
            director="David Kowalski",
        )
        self.assertFalse(is_tmdb_compatible(film_a_no_id.identification, same_title_other_dir))

        # 4) apply_tmdb_verification preserves model_identification and catalogue_identification separately
        #    and NEVER substitutes Film B's IDs onto Film A's title/ratings!
        merged = apply_tmdb_verification(film_a_payload, film_b_tmdb)
        self.assertEqual(merged.identification.canonical_title, "Paper Harbor")
        self.assertEqual(merged.identification.tmdb_id, 50101)
        self.assertIsNone(merged.identification.imdb_id)
        self.assertEqual(merged.identification.match_confidence, "low")
        self.assertIsNotNone(merged.model_identification)
        assert merged.model_identification is not None
        self.assertEqual(merged.model_identification.canonical_title, "Paper Harbor")
        self.assertEqual(merged.model_identification.tmdb_id, 50101)
        self.assertIsNotNone(merged.catalogue_identification)
        assert merged.catalogue_identification is not None
        self.assertFalse(merged.catalogue_identification.compatible)
        self.assertEqual(merged.catalogue_identification.tmdb_id, 50202)
        self.assertEqual(merged.catalogue_identification.imdb_id, "tt5020200")
        self.assertIsNotNone(merged.catalogue_identification.conflict_reason)

        # 5) End-to-end /evaluate withholds caching when catalogue conflict occurs
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = TriageDB(Path(tmpdir) / "triage.db")
            test_db.init_db()
            mock_tmdb = TmdbClient(api_key="dummy-key")
            mock_tmdb.lookup_with_diagnostics = AsyncMock(  # type: ignore[method-assign]
                return_value=TmdbLookupResult(movie=film_b_tmdb, trusted_for_cache=True)
            )
            mock_tmdb.verify_identification = AsyncMock(return_value=None)  # type: ignore[method-assign]
            mock_gemini = AsyncMock(return_value=film_a_payload)
            with (
                patch.object(main, "db", test_db),
                patch.object(main, "tmdb_client", mock_tmdb),
                patch.object(main.gemini_client, "evaluate", mock_gemini),
            ):
                resp = asyncio.run(
                    main.evaluate(EvaluateRequest(raw_text="Paper.Harbor.2024.2160p.UHD.BluRay.REMUX-GRP"))
                )
                self.assertFalse(resp.cached)
                self.assertEqual(resp.identification.tmdb_id, 50101)
                self.assertIsNone(resp.identification.imdb_id)
                self.assertIsNotNone(resp.catalogue_identification)
                assert resp.catalogue_identification is not None
                self.assertFalse(resp.catalogue_identification.compatible)
                # Conflict result must NOT be cached
                self.assertEqual(test_db.get_cache_entries_count(), 0)

    def test_issue_13_4d_edition_aware_cache_and_exact_only_early_lookup(self) -> None:
        """Issue #13 §4d: Theatrical vs Extended cut with identical movie/group/quality must not share cache."""
        import main
        from db import extract_edition_key
        from tmdb_client import TmdbLookupResult

        self.assertEqual(
            extract_edition_key("Example.Film.2024.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES"), ""
        )
        self.assertEqual(
            extract_edition_key("Example.Film.2024.Extended.Cut.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES"),
            "extended",
        )
        self.assertEqual(
            extract_edition_key("Example.Film.2024.Directors.Cut.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES"),
            "directors_cut",
        )
        self.assertEqual(
            extract_edition_key("Example.Film.2024.Theatrical.Cut.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES"),
            "theatrical",
        )

        tmdb_movie = TmdbMovie(
            tmdb_id=88001,
            canonical_title="Example Film",
            original_title="Example Film",
            year=2024,
            imdb_id="tt8800100",
            runtime_minutes=120,
            director="Elena Vance",
        )
        eval_theatrical = _sample_payload(
            title="Example Film", year=2024, director="Elena Vance", imdb_id="tt8800100", tmdb_id=88001
        )
        eval_extended = eval_theatrical.model_copy(
            update={
                "decision_aid": eval_theatrical.decision_aid.model_copy(
                    update={"verdict_reason": "Extended cut adds 24 minutes of restored scenes."}
                )
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = TriageDB(Path(tmpdir) / "triage.db")
            test_db.init_db()
            mock_tmdb = TmdbClient(api_key="dummy-key")
            mock_tmdb.lookup_with_diagnostics = AsyncMock(  # type: ignore[method-assign]
                return_value=TmdbLookupResult(movie=tmdb_movie, trusted_for_cache=True)
            )
            mock_tmdb.verify_identification = AsyncMock(return_value=tmdb_movie)  # type: ignore[method-assign]
            mock_gemini = AsyncMock(side_effect=[eval_theatrical, eval_extended])

            with (
                patch.object(main, "db", test_db),
                patch.object(main, "tmdb_client", mock_tmdb),
                patch.object(main.gemini_client, "evaluate", mock_gemini),
            ):
                # 1) Theatrical cut (no supplemental torrent_info)
                r_theat = asyncio.run(
                    main.evaluate(
                        EvaluateRequest(
                            raw_text="Example.Film.2024.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES"
                        )
                    )
                )
                self.assertFalse(r_theat.cached)
                self.assertEqual(mock_gemini.await_count, 1)

                # 2) Extended cut with identical movie, group, quality, and NO supplemental torrent_info
                #    MUST miss both early exact lookup and post-TMDB lookup!
                r_ext = asyncio.run(
                    main.evaluate(
                        EvaluateRequest(
                            raw_text="Example.Film.2024.Extended.Cut.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES"
                        )
                    )
                )
                self.assertFalse(r_ext.cached)
                self.assertEqual(mock_gemini.await_count, 2)
                self.assertIn("Extended cut", r_ext.decision_aid.verdict_reason)

                # 3) Repeating exact Extended cut hits cache cleanly
                r_ext_repeat = asyncio.run(
                    main.evaluate(
                        EvaluateRequest(
                            raw_text="Example.Film.2024.Extended.Cut.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES"
                        )
                    )
                )
                self.assertTrue(r_ext_repeat.cached)
                self.assertEqual(mock_gemini.await_count, 2)

                # 4) Early lookup does NOT reuse another release string before independent identification
                mock_tmdb.lookup_with_diagnostics = AsyncMock(  # type: ignore[method-assign]
                    return_value=TmdbLookupResult(movie=None, trusted_for_cache=False)
                )
                mock_gemini.side_effect = [eval_theatrical]
                r_unverified_other = asyncio.run(
                    main.evaluate(
                        EvaluateRequest(
                            raw_text="Example.Film.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-CiNEPHiLES"
                        )
                    )
                )
                self.assertFalse(r_unverified_other.cached)
                self.assertEqual(mock_gemini.await_count, 3)

    def test_issue_13_4e_feedback_preserves_tmdb_id_and_distinct_same_title_films(self) -> None:
        """Issue #13 §4e: Feedback stores tmdb_id and keeps two same-title films separate when IMDb is absent."""
        import sqlite3
        import main
        from schemas import FeedbackRequest

        with tempfile.TemporaryDirectory() as tmpdir:
            # Verify additive migration on a legacy feedback table lacking tmdb_id
            db_path = Path(tmpdir) / "triage.db"
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE feedback (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        imdb_id TEXT,
                        title TEXT NOT NULL,
                        decision TEXT NOT NULL CHECK (decision IN ('WATCH', 'MAYBE', 'SKIP')),
                        note TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
            test_db = TriageDB(db_path)
            test_db.init_db()

            with patch.object(main, "db", test_db):
                r1 = asyncio.run(
                    main.submit_feedback(
                        FeedbackRequest(
                            imdb_id=None,
                            tmdb_id=7001,
                            title="Same Title Film",
                            decision="watch",  # type: ignore[arg-type]
                            note="watched",
                        )
                    )
                )
                self.assertEqual(r1.status, "saved")
                self.assertEqual(r1.decision, "WATCH")
                self.assertEqual(r1.total_feedback, 1)

                r2 = asyncio.run(
                    main.submit_feedback(
                        FeedbackRequest(
                            imdb_id=None,
                            tmdb_id=7002,
                            title="Same Title Film",
                            decision="skip",  # type: ignore[arg-type]
                            note="watched",
                        )
                    )
                )
                self.assertEqual(r2.status, "saved")
                self.assertEqual(r2.decision, "SKIP")
                self.assertEqual(r2.total_feedback, 2)

                items = asyncio.run(main.list_feedback(limit=10))
                by_tmdb = {item.tmdb_id: item for item in items}
                self.assertEqual(set(by_tmdb.keys()), {7001, 7002})
                self.assertEqual(by_tmdb[7001].decision, "WATCH")
                self.assertEqual(by_tmdb[7001].note, "watched")
                self.assertEqual(by_tmdb[7002].decision, "SKIP")
                self.assertEqual(by_tmdb[7002].note, "watched")

                # Re-rating tmdb_id=7001 updates ONLY 7001 and preserves 7002
                r3 = asyncio.run(
                    main.submit_feedback(
                        FeedbackRequest(
                            imdb_id=None,
                            tmdb_id=7001,
                            title="Same Title Film",
                            decision="skip",  # type: ignore[arg-type]
                            note="watched",
                        )
                    )
                )
                self.assertEqual(r3.total_feedback, 2)

    def test_issue_13_6d_rating_range_bounds_and_grounding_source_verification(self) -> None:
        """Issue #13 §6d & PR #18 Gap 3: Enforce numeric rating bounds and strict film/metric/value grounding attribution."""
        import json
        from types import SimpleNamespace
        from pydantic import ValidationError
        from gemini_client import GeminiTriageClient, validate_and_ground_ratings

        # 1) Schema enforces platform-specific numeric ranges
        for bad_kwargs in (
            {"rotten_tomatoes_critics": 101},
            {"rotten_tomatoes_critics": -1},
            {"rotten_tomatoes_audience": 120},
            {"metacritic": 105},
            {"imdb": 10.5},
            {"imdb": -0.1},
            {"letterboxd": 5.5},
            {"letterboxd": -0.5},
        ):
            with self.assertRaises(ValidationError):
                Ratings(**bad_kwargs)  # type: ignore[arg-type]

        # 2) Out-of-range, ungrounded, and third-party interview mentions become None (unknown);
        #    only ratings backed by authoritative domain + actual film + metric + value are retained.
        ident = {"canonical_title": "Example Film", "year": 2024, "imdb_id": "tt1234567"}
        raw_ratings = {
            "rotten_tomatoes_critics": 94,       # grounded (rottentomatoes.com + film + Tomatometer 94%) -> kept
            "rotten_tomatoes_audience": 115,     # out of range -> None
            "metacritic": 88,                    # in range but NO metacritic grounding chunk -> None
            "imdb": 8.1,                         # grounded (imdb.com/title/tt1234567 + IMDb 8.1/10) -> kept
            "letterboxd": 6.2,                   # out of range (>5.0) -> None
            "missing_sources": [],
        }
        grounding = [
            {
                "domain": "rottentomatoes.com",
                "path": "/m/example_film",
                "title": "Example Film | Rotten Tomatoes",
                "snippet": "Example Film (2024): 94% Tomatometer critics rating",
                "supported_segments": ['"rotten_tomatoes_critics": 94'],
            },
            {
                "domain": "imdb.com",
                "path": "/title/tt1234567/",
                "title": "Example Film (2024) - IMDb",
                "snippet": "IMDb rating 8.1/10",
                "supported_segments": ['"imdb": 8.1'],
            },
            {
                "domain": "en.wikipedia.org",
                "path": "/wiki/Example_Film",
                "title": "Example Film - Wikipedia",
                "snippet": "",
                "supported_segments": [],
            },
        ]
        cleaned = validate_and_ground_ratings(raw_ratings, grounding_sources=grounding, identification=ident)
        self.assertEqual(cleaned["rotten_tomatoes_critics"], 94)
        self.assertIsNone(cleaned["rotten_tomatoes_audience"])
        self.assertIsNone(cleaned["metacritic"])
        self.assertEqual(cleaned["imdb"], 8.1)
        self.assertIsNone(cleaned["letterboxd"])
        self.assertEqual(
            cleaned["sources"],
            {"rotten_tomatoes_critics": "rottentomatoes.com", "imdb": "imdb.com"},
        )
        self.assertIn("Rotten Tomatoes Audience", cleaned["missing_sources"])
        self.assertIn("Metacritic", cleaned["missing_sources"])
        self.assertIn("Letterboxd", cleaned["missing_sources"])

        # 2b) PR #18 Gap 3 negative cases:
        #     - Unrelated third-party interview mentioning IMDb credits cannot retain invented 9.2 score!
        #     - Authoritative imdb.com page WITHOUT score evidence (or wrong film tt ID) cannot retain 9.2!
        weak_grounding = [
            {
                "domain": "filminterviews.example",
                "path": "/director-interview",
                "title": "Director Interview on IMDb Credits and Example Film",
                "snippet": "Discussing IMDb credits and career highlights (9.2 out of 10 excitement).",
                "supported_segments": ['"imdb": 9.2'],
            },
            {
                "domain": "imdb.com",
                "path": "/title/tt1234567/",
                "title": "Example Film (2024) - IMDb",
                "snippet": "Directed by Elena Vance. Full cast and crew.",
                "supported_segments": [],
            },
            {
                "domain": "imdb.com",
                "path": "/title/tt9999999/",
                "title": "Other Film (2024) - IMDb",
                "snippet": "IMDb rating 9.2/10",
                "supported_segments": ['"imdb": 9.2'],
            },
        ]
        cleaned_weak = validate_and_ground_ratings(
            {"imdb": 9.2, "missing_sources": []},
            grounding_sources=weak_grounding,
            identification=ident,
        )
        self.assertIsNone(cleaned_weak["imdb"])
        self.assertEqual(cleaned_weak["sources"], {})
        self.assertIn("IMDb", cleaned_weak["missing_sources"])

        # 3) End-to-end GeminiTriageClient._call_sync enforces per-metric grounding_supports
        fake_payload_json = json.dumps(
            {
                "identification": {
                    "canonical_title": "Example Film",
                    "year": 2024,
                    "director": "Elena Vance",
                    "imdb_id": "tt1234567",
                    "tmdb_id": 12345,
                    "runtime_minutes": 110,
                    "genres": ["Drama"],
                    "match_confidence": "high",
                },
                "ratings": {
                    "rotten_tomatoes_critics": 92,
                    "rotten_tomatoes_audience": 85,
                    "metacritic": 80,
                    "imdb": 11.4,
                    "letterboxd": 4.2,
                    "missing_sources": [],
                },
                "recognition": {"awards_highlights": [], "critic_consensus": "Well received."},
                "decision_aid": {
                    "one_line_pitch": "Sharp drama.",
                    "similar_to": ["Film X"],
                    "red_flags": [],
                    "verdict": "WATCH",
                    "verdict_reason": "Strong craft.",
                },
            }
        )
        fake_response = SimpleNamespace(
            text=fake_payload_json,
            candidates=[
                SimpleNamespace(
                    grounding_metadata=SimpleNamespace(
                        grounding_chunks=[
                            SimpleNamespace(
                                web=SimpleNamespace(
                                    uri="https://www.rottentomatoes.com/m/example_film",
                                    title="Example Film - Rotten Tomatoes",
                                )
                            ),
                            SimpleNamespace(
                                web=SimpleNamespace(
                                    uri="https://interviews.example/elena-vance-imdb-credits",
                                    title="Elena Vance Interview on IMDb Credits",
                                )
                            ),
                        ],
                        grounding_supports=[
                            SimpleNamespace(
                                segment=SimpleNamespace(text='"rotten_tomatoes_critics": 92'),
                                grounding_chunk_indices=[0],
                            )
                        ],
                    )
                )
            ],
        )
        fake_sdk = SimpleNamespace(
            models=SimpleNamespace(generate_content=lambda **kwargs: fake_response)
        )
        gclient = GeminiTriageClient(api_key="dummy-test-key")
        with patch.object(gclient, "_get_client", return_value=fake_sdk):
            parsed = gclient._call_sync(EvaluateRequest(raw_text="Example.Film.2024"), "sys")
            # Critics 92 is backed by rottentomatoes.com/m/example_film + grounding_supports -> kept
            self.assertEqual(parsed.ratings.rotten_tomatoes_critics, 92)
            # Audience 85 was NOT grounded in grounding_supports or snippet -> stripped to None!
            self.assertIsNone(parsed.ratings.rotten_tomatoes_audience)
            self.assertIsNone(parsed.ratings.metacritic)
            self.assertIsNone(parsed.ratings.imdb)
            self.assertIsNone(parsed.ratings.letterboxd)
            self.assertEqual(
                parsed.ratings.sources,
                {"rotten_tomatoes_critics": "rottentomatoes.com"},
            )

    def test_issue_13_5a_every_outbound_prompt_field_redacts_secret_markers(self) -> None:
        """Issue #13 §5a: Every outbound LLM prompt field scrubs secret markers without mangling normal titles."""
        from gemini_client import build_system_instruction, build_user_prompt
        from schemas import TorrentInfo
        from tmdb_client import format_torrent_info_for_prompt, sanitize_tracker_text

        markers = [
            "MARK_RAW_PASS",
            "MARK_HINT_KEY",
            "MARK_EXTRA_COOK",
            "MARK_RQ_TOK",
            "MARK_IDX_URL",
            "MARK_CAT_KEY",
            "MARK_RES_SEC",
            "MARK_SRC_BEAR",
            "MARK_PROC_WH",
            "MARK_DR_ACT",
            "MARK_GRP_BOT",
            "MARK_AUD_AIZA",
            "MARK_IMDB_LEAK",
            "MARK_DESC_JWT",
            "MARK_NFO_PASS",
            "MARK_WARN_TOK",
            "MARK_FB_TITLE",
            "MARK_FB_NOTE",
        ]
        tinfo = TorrentInfo(
            indexer="TorrentLeech https://tracker.invalid/MARK_IDX_URL",
            category="Movies :: 4K api_key=MARK_CAT_KEY",
            resolution="2160p secret=MARK_RES_SEC",
            source="UHD BluRay Bearer MARK_SRC_BEAR_123456",
            media_processing="REMUX /webhook/MARK_PROC_WH",
            dynamic_range=["HDR10 /action/MARK_DR_ACT"],
            group="GRP 123456789:AAH_MARK_GRP_BOT_0123456789",
            audio_languages=["English AIzaSyMARK_AUD_AIZA_1234567890"],
            imdb_id="tt0000000 passkey=MARK_IMDB_LEAK",
            description="Synopsis eyJhbGciOiJIUzI1NiJ9.eyJtIjoiTUFSS19ERVNDX0pXVCJ9.MARK_DESC_JWT",
            nfo_text="Duration: 1 h 55 min passkey=MARK_NFO_PASS",
        )
        req = EvaluateRequest(
            raw_text="Example.Film.2025.2160p.UHD.BluRay.REMUX passkey=MARK_RAW_PASS",
            hint_title="Example Film api_key=MARK_HINT_KEY",
            hint_year=2025,
            extra_context="cookie=MARK_EXTRA_COOK",
            torrent_info=tinfo,
        )
        torrent_ctx = format_torrent_info_for_prompt(
            tinfo,
            conflict_warning="Mismatch token=MARK_WARN_TOK",
        )
        user_prompt = build_user_prompt(
            req,
            torrent_context=torrent_ctx,
            release_quality_context="Release quality: Remux-2160p token=MARK_RQ_TOK",
        )
        sys_prompt = build_system_instruction(
            "Taste profile",
            [
                {
                    "decision": "WATCH",
                    "title": "Harbor Film passkey=MARK_FB_TITLE",
                    "imdb_id": "tt1234567",
                    "note": "watched cookie=MARK_FB_NOTE",
                }
            ],
        )
        combined = f"{user_prompt}\n{sys_prompt}"
        for m in markers:
            self.assertNotIn(m, combined, f"Secret marker {m} leaked into outbound LLM prompt!")

        # Non-regression: ordinary movie titles containing 'Secret' or 'Cookie' MUST NOT be mangled!
        self.assertEqual(
            sanitize_tracker_text("The.Secret.Agent.2025.2160p.UHD.BluRay.REMUX-GRP"),
            "The.Secret.Agent.2025.2160p.UHD.BluRay.REMUX-GRP",
        )
        self.assertEqual(
            sanitize_tracker_text("The Secret Life of Walter Mitty"),
            "The Secret Life of Walter Mitty",
        )
        self.assertEqual(sanitize_tracker_text("Cookie's Fortune"), "Cookie's Fortune")

    def test_pr18_blocker_1_model_suggestion_cannot_replace_release_identity_or_poison_cache(self) -> None:
        """PR #18 Blocker 1: Release-grounded film A vs competing model film B returns conflict and never caches B."""
        import main
        from schemas import TorrentInfo

        request = EvaluateRequest(
            raw_text="North.Field.2021.1080p.BluRay.REMUX-GROUP",
            hint_title="North Field",
            hint_year=2021,
            torrent_info=TorrentInfo(tmdb_id=445001, imdb_id="tt4440001"),
        )
        film_a = TmdbMovie(
            tmdb_id=445001,
            canonical_title="North Field",
            original_title="North Field",
            year=2021,
            imdb_id="tt4440001",
            director="Alex Green",
        )
        film_b = TmdbMovie(
            tmdb_id=445002,
            canonical_title="Empty View",
            original_title="Empty View",
            year=2021,
            imdb_id="tt4440002",
            director="Alex May",
        )
        model_b = _sample_payload(
            title=film_b.canonical_title,
            year=2021,
            director=film_b.director or "",
            tmdb_id=film_b.tmdb_id,
            imdb_id=film_b.imdb_id,
        )
        catalogue = TmdbClient(api_key="fixture")
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = TriageDB(Path(tmpdir) / "triage.db")
            test_db.init_db()
            mock_eval = AsyncMock(return_value=model_b)
            with (
                patch.object(
                    catalogue,
                    "_find_tmdb_id_by_imdb",
                    side_effect={film_a.imdb_id: film_a.tmdb_id, film_b.imdb_id: film_b.tmdb_id}.get,
                ),
                patch.object(
                    catalogue,
                    "_fetch_movie_details",
                    side_effect={film_a.tmdb_id: film_a, film_b.tmdb_id: film_b}.get,
                ),
                patch.object(catalogue, "_search_results", return_value=[]),
                patch.object(main, "tmdb_client", catalogue),
                patch.object(main, "db", test_db),
                patch.object(main.gemini_client, "evaluate", new=mock_eval),
            ):
                first = asyncio.run(main.evaluate(request))
                second = asyncio.run(main.evaluate(request))

            self.assertFalse(first.cached)
            self.assertIsNotNone(first.catalogue_identification)
            assert first.catalogue_identification is not None
            self.assertEqual(first.catalogue_identification.tmdb_id, 445001)
            self.assertEqual(first.catalogue_identification.imdb_id, "tt4440001")
            self.assertFalse(first.catalogue_identification.compatible)
            self.assertIsNotNone(first.catalogue_identification.conflict_reason)
            self.assertIsNotNone(first.model_identification)
            assert first.model_identification is not None
            self.assertEqual(first.model_identification.tmdb_id, 445002)
            self.assertEqual(first.identification.match_confidence, "low")

            # Repeat request MUST NOT serve competing film B as cached or compatible!
            self.assertFalse(second.cached)
            self.assertIsNotNone(second.catalogue_identification)
            assert second.catalogue_identification is not None
            self.assertFalse(second.catalogue_identification.compatible)
            self.assertEqual(second.catalogue_identification.tmdb_id, 445001)
            self.assertEqual(mock_eval.await_count, 2)
            self.assertEqual(test_db.get_cache_entries_count(), 0)

    def test_pr18_gap_2_feedback_partial_id_updates_preserve_verified_links(self) -> None:
        """PR #18 Gap 2: Partial TMDb-only then IMDb-only updates keep one unified row preserving both IDs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = TriageDB(Path(tmpdir) / "triage.db")
            test_db.init_db()

            cnt1 = test_db.add_feedback(
                imdb_id="tt4450010",
                tmdb_id=445001,
                title="North Field",
                decision="WATCH",
                note="watched",
            )
            self.assertEqual(cnt1, 1)

            cnt2 = test_db.add_feedback(
                imdb_id=None,
                tmdb_id=445001,
                title="North Field",
                decision="SKIP",
                note="watched",
            )
            self.assertEqual(cnt2, 1)
            rows2 = test_db.get_recent_feedback(10)
            self.assertEqual(len(rows2), 1)
            self.assertEqual(rows2[0]["imdb_id"], "tt4450010")
            self.assertEqual(rows2[0]["tmdb_id"], 445001)
            self.assertEqual(rows2[0]["decision"], "SKIP")

            cnt3 = test_db.add_feedback(
                imdb_id="tt4450010",
                tmdb_id=None,
                title="North Field",
                decision="WATCH",
                note="watched",
            )
            self.assertEqual(cnt3, 1)
            rows3 = test_db.get_recent_feedback(10)
            self.assertEqual(len(rows3), 1)
            self.assertEqual(rows3[0]["imdb_id"], "tt4450010")
            self.assertEqual(rows3[0]["tmdb_id"], 445001)
            self.assertEqual(rows3[0]["decision"], "WATCH")

    def test_pr18_r2_blocker_2_and_gaps_unresolved_conflict_ratings_edition_and_privacy(self) -> None:
        """
        PR #18 Round 2 Blocker 2, Blocker 5 (Triage boundary), and Gaps 2 & 3:
          - Unresolved tracker identity conflict with no title-search match cannot self-verify or cache model proposal.
          - Swapped RT critic/audience ratings and wrong-year remake ratings are rejected; SDK web.domain on Vertex redirect is accepted.
          - Restoration year after release year preserves Extended vs Theatrical cut distinction in edition_key and IMDb cache.
          - Credential-like markers in accepted hint_title are scrubbed from build_user_prompt and extract_tmdb_query.
        """
        import main
        from google.genai import types as sdk
        from db import extract_edition_key
        from gemini_client import build_user_prompt, extract_grounding_sources, validate_and_ground_ratings
        from schemas import TorrentInfo
        from tmdb_client import extract_tmdb_query

        # 1) Blocker 2: Unresolved tracker conflict + no release-title match -> incompatible & uncached
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = TriageDB(Path(tmpdir) / "triage.db")
            test_db.init_db()
            movie_97 = TmdbMovie(
                tmdb_id=97,
                imdb_id="tt7777777",
                canonical_title="Glass Harbor",
                year=2024,
                director="A. Blue",
            )
            movie_98 = TmdbMovie(
                tmdb_id=98,
                imdb_id="tt1111111",
                canonical_title="Indigo Mist",
                year=2020,
                director="R. Hill",
            )
            proposed = _sample_payload(
                title="Glass Harbor",
                year=2024,
                imdb_id="tt7777777",
                tmdb_id=97,
                director="A. Blue",
            )
            req = EvaluateRequest(
                raw_text="Amber.Ridge.2004.1080p.BluRay.REMUX-FIXTURE",
                torrent_info=TorrentInfo(tmdb_id=97, imdb_id="tt1111111", runtime_minutes=102),
            )
            client = TmdbClient(api_key="invented")
            client._fetch_movie_details = lambda i: {97: movie_97, 98: movie_98}[i]  # type: ignore[method-assign]
            client._find_tmdb_id_by_imdb = lambda i: {"tt7777777": 97, "tt1111111": 98}.get(i)  # type: ignore[method-assign]
            client._search_tmdb_movie = lambda *args, **kwargs: None  # type: ignore[method-assign]
            client.lookup_with_diagnostics = AsyncMock(wraps=client.lookup_with_diagnostics)  # type: ignore[method-assign]

            with (
                patch.object(main, "db", test_db),
                patch.object(main, "tmdb_client", client),
                patch.object(main.gemini_client, "evaluate", new=AsyncMock(return_value=proposed)),
            ):
                first = asyncio.run(main.evaluate(req))
                second = asyncio.run(main.evaluate(req))

            self.assertFalse(first.cached)
            assert first.catalogue_identification is not None
            self.assertFalse(first.catalogue_identification.compatible)
            self.assertEqual(first.identification.match_confidence, "low")
            self.assertFalse(second.cached)
            assert second.catalogue_identification is not None
            self.assertFalse(second.catalogue_identification.compatible)
            self.assertEqual(client.lookup_with_diagnostics.await_count, 2)
            self.assertEqual(test_db.get_cache_entries_count(), 0)

        # 2) Gap 2: Swapped critic/audience scores, wrong-year remake, and SDK explicit domain on Vertex redirect
        ident = {"canonical_title": "Amber Ridge", "year": 2024, "imdb_id": "tt1111111"}

        def _grounded(web: dict[str, str], segment: str, ratings: dict[str, int | float]) -> dict[str, object]:
            resp = sdk.GenerateContentResponse(
                candidates=[
                    {
                        "grounding_metadata": {
                            "grounding_chunks": [{"web": web}],
                            "grounding_supports": [
                                {"segment": {"text": segment}, "grounding_chunk_indices": [0]}
                            ],
                        }
                    }
                ]
            )
            return validate_and_ground_ratings(
                ratings,
                grounding_sources=extract_grounding_sources(resp),
                identification=ident,
            )

        rt_web = {"uri": "https://www.rottentomatoes.com/m/amber_ridge_2024", "title": "Amber Ridge (2024)"}
        rt_seg = "Amber Ridge (2024) has a Tomatometer of 42% and a Popcornmeter of 89%."
        swapped = _grounded(rt_web, rt_seg, {"rotten_tomatoes_critics": 89, "rotten_tomatoes_audience": 42})
        self.assertIsNone(swapped["rotten_tomatoes_critics"])
        self.assertIsNone(swapped["rotten_tomatoes_audience"])
        self.assertEqual(swapped["sources"], {})

        aligned = _grounded(rt_web, rt_seg, {"rotten_tomatoes_critics": 42, "rotten_tomatoes_audience": 89})
        self.assertEqual(aligned["rotten_tomatoes_critics"], 42)
        self.assertEqual(aligned["rotten_tomatoes_audience"], 89)
        self.assertEqual(
            aligned["sources"],
            {
                "rotten_tomatoes_critics": "rottentomatoes.com",
                "rotten_tomatoes_audience": "rottentomatoes.com",
            },
        )

        rt_1983 = {"uri": "https://www.rottentomatoes.com/m/amber_ridge_1983", "title": "Amber Ridge (1983)"}
        remake_res = _grounded(rt_1983, "Amber Ridge (1983) has a Tomatometer of 86%.", {"rotten_tomatoes_critics": 86})
        self.assertIsNone(remake_res["rotten_tomatoes_critics"])
        self.assertEqual(remake_res["sources"], {})

        redirect_web = {
            "domain": "imdb.com",
            "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/fixture",
            "title": "Amber Ridge (2024)",
        }
        redirect_res = _grounded(redirect_web, "Amber Ridge tt1111111 has an IMDb score of 7.5/10.", {"imdb": 7.5})
        self.assertEqual(redirect_res["imdb"], 7.5)
        self.assertEqual(redirect_res["sources"], {"imdb": "imdb.com"})

        # 3) Gap 3: Restoration year after release year preserves Extended vs Theatrical cut distinction
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = TriageDB(Path(tmpdir) / "triage.db")
            test_db.init_db()
            rel_ext = "Amber.Ridge.2004.Extended.2020.Restoration.1080p.BluRay.REMUX-ZETA"
            rel_th = "Amber.Ridge.2004.Theatrical.2020.Restoration.1080p.BluRay.REMUX-ZETA"
            ek_ext = extract_edition_key(rel_ext)
            ek_th = extract_edition_key(rel_th)
            self.assertIn("extended", ek_ext)
            self.assertIn("theatrical", ek_th)
            self.assertNotEqual(ek_ext, ek_th)
            test_db.save_evaluation(
                rel_ext,
                _sample_payload(title="Amber Ridge", year=2004, imdb_id="tt1111111").model_dump(),
            )
            self.assertIsNone(
                test_db.get_cached_evaluation(
                    rel_th,
                    hint_year=2004,
                    extra_context="tt1111111",
                    allow_release_key_fallback=False,
                )
            )

        # 4) Blocker 5 (Triage boundary): Synthetic credential markers in accepted hint_title
        markers = [
            "SYNTH_ACCESS_TOK_11",
            "SYNTH_QUOTED_PASS_22",
            "SYNTH_BEARER_33",
            "SYNTH_PHPSESS_44",
            "SYNTH_TLPASS_55",
            "SYNTH_OPENAI_KEY_66778899",
            "SYNTH_USERPASS_77",
        ]
        dirty_hint = (
            "Amber Ridge "
            f"access_token={markers[0]} "
            f'passkey="{markers[1]}" '
            f"Authorization: Bearer {markers[2]} "
            f"Cookie: PHPSESSID={markers[3]}; tlpass={markers[4]} "
            f"sk-proj-{markers[5]} "
            f"user:{markers[6]}@tracker.invalid"
        )
        prompt = build_user_prompt(
            EvaluateRequest(
                raw_text="Amber.Ridge.2024.1080p.BluRay.REMUX-GRP",
                hint_title=dirty_hint,
                hint_year=2024,
            )
        )
        q_title, _q_year = extract_tmdb_query(
            "Amber.Ridge.2024.1080p.BluRay.REMUX-GRP",
            hint_title=dirty_hint,
            hint_year=2024,
        )
        for m in markers:
            self.assertNotIn(m, prompt)
            self.assertNotIn(m, q_title or "")
        # Normal movie titles remain completely intact
        clean_prompt = build_user_prompt(
            EvaluateRequest(
                raw_text="The.Secret.Life.of.Walter.Mitty.2013.1080p.BluRay.REMUX-GRP",
                hint_title="The Secret Life of Walter Mitty",
                hint_year=2013,
            )
        )
        self.assertIn("The Secret Life of Walter Mitty", clean_prompt)

    def test_pr18_r3_findings_3_4_and_5_late_remake_cache_invalidation_and_review_count(self) -> None:
        """
        PR #18 Round 3 Findings 3, 4, and 5 (Triage):
          - Finding 3: Late model verification checks the candidate TMDb record against
            original release evidence (year/runtime/title), rejecting a 2024/125m remake for
            a 2012/96m release while accepting and caching a genuinely matching 2012/96m film.
          - Finding 4: A forced refresh that establishes a catalogue contradiction invalidates
            earlier reachable cached compatible entries (exact + IMDb alias) while preserving
            unrelated movies/years.
          - Finding 5: RT percentage attribution accepts '82%' while rejecting review counts
            such as '74 critic reviews' or '1,250 verified audience ratings'.
        """
        from google.genai import types as sdk
        import main
        from gemini_client import extract_grounding_sources, validate_and_ground_ratings
        from schemas import TorrentInfo

        # 1) Finding 5: Review count vs percentage attribution
        ident = {"canonical_title": "Crystal Harbor", "year": 2012, "imdb_id": "tt1111111"}

        def _grounded(segment: str, ratings: dict[str, int | float], site: str = "rt") -> dict[str, object]:
            if site == "rt":
                web = {"uri": "https://www.rottentomatoes.com/m/crystal_harbor_2012", "title": "Crystal Harbor (2012)"}
            else:
                web = {
                    "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/fixture",
                    "domain": "imdb.com",
                    "title": "Crystal Harbor (2012)",
                }
            resp = sdk.GenerateContentResponse(
                candidates=[
                    {
                        "grounding_metadata": {
                            "grounding_chunks": [{"web": web}],
                            "grounding_supports": [
                                {"segment": {"text": segment}, "grounding_chunk_indices": [0]}
                            ],
                        }
                    }
                ]
            )
            return validate_and_ground_ratings(
                ratings,
                grounding_sources=extract_grounding_sources(resp),
                identification=ident,
            )

        rt_seg = "Crystal Harbor has a Tomatometer of 82% based on 74 critic reviews."
        res_82 = _grounded(rt_seg, {"rotten_tomatoes_critics": 82})
        self.assertEqual(res_82["rotten_tomatoes_critics"], 82)
        self.assertEqual(res_82["sources"], {"rotten_tomatoes_critics": "rottentomatoes.com"})

        res_74 = _grounded(rt_seg, {"rotten_tomatoes_critics": 74})
        self.assertIsNone(res_74["rotten_tomatoes_critics"])
        self.assertEqual(res_74["sources"], {})

        aud_seg = "Crystal Harbor has a Popcornmeter of 88% across 1,250 verified audience ratings."
        self.assertEqual(_grounded(aud_seg, {"rotten_tomatoes_audience": 88})["rotten_tomatoes_audience"], 88)
        self.assertIsNone(_grounded(aud_seg, {"rotten_tomatoes_audience": 1250})["rotten_tomatoes_audience"])

        imdb_pos = _grounded("Crystal Harbor: IMDb rating 7.4/10.", {"imdb": 7.4}, site="imdb")
        self.assertEqual(imdb_pos["imdb"], 7.4)
        self.assertEqual(imdb_pos["sources"], {"imdb": "imdb.com"})

        # 2) Finding 3: Late verification against original release evidence (all 4 controls/cases)
        original = TmdbMovie(
            tmdb_id=31,
            imdb_id="tt1111111",
            canonical_title="Crystal Harbor",
            year=2012,
            director="Anna Linn",
            runtime_minutes=96,
        )
        remake = TmdbMovie(
            tmdb_id=95,
            imdb_id="tt7777777",
            canonical_title="Crystal Harbor",
            year=2024,
            director="Julie Quinn",
            runtime_minutes=125,
        )
        for kind in ("conflicting-pair", "anchored-contradiction", "late-correct", "late-remake"):
            with tempfile.TemporaryDirectory() as tmpdir:
                test_db = TriageDB(Path(tmpdir) / "triage.db")
                test_db.init_db()
                proposed = original if kind == "late-correct" else remake
                model_res = _sample_payload(
                    title=proposed.canonical_title,
                    year=proposed.year,
                    director=proposed.director,
                    imdb_id=proposed.imdb_id,
                    tmdb_id=proposed.tmdb_id,
                    runtime_minutes=proposed.runtime_minutes,
                )
                rel_tinfo: dict[str, object] = {"runtime_minutes": 96}
                if kind == "conflicting-pair":
                    rel_tinfo.update({"tmdb_id": 95, "imdb_id": "tt1111111"})
                req = EvaluateRequest(
                    raw_text="Crystal.Harbor.2012.1080p.BluRay.REMUX-FIXTURE",
                    hint_title="Crystal Harbor",
                    hint_year=2012,
                    torrent_info=TorrentInfo(**rel_tinfo),  # type: ignore[arg-type]
                )
                client = TmdbClient(api_key="invented")
                client._fetch_movie_details = lambda i: {31: original, 95: remake}[i]  # type: ignore[method-assign]
                client._find_tmdb_id_by_imdb = lambda i: {"tt1111111": 31, "tt7777777": 95}.get(i)  # type: ignore[method-assign]
                client._search_tmdb_movie = (  # type: ignore[method-assign]
                    (lambda *args, **kwargs: original) if kind == "anchored-contradiction" else (lambda *args, **kwargs: None)
                )
                client.lookup_with_diagnostics = AsyncMock(wraps=client.lookup_with_diagnostics)  # type: ignore[method-assign]

                with (
                    patch.object(main, "db", test_db),
                    patch.object(main, "tmdb_client", client),
                    patch.object(main.gemini_client, "evaluate", new=AsyncMock(return_value=model_res)),
                ):
                    one = asyncio.run(main.evaluate(req))
                    two = asyncio.run(main.evaluate(req))

                assert one.catalogue_identification is not None
                assert two.catalogue_identification is not None
                if kind == "late-correct":
                    self.assertTrue(one.catalogue_identification.compatible, kind)
                    self.assertTrue(two.cached, kind)
                    self.assertEqual(two.identification.year, 2012, kind)
                    self.assertEqual(client.lookup_with_diagnostics.await_count, 1, kind)
                else:
                    self.assertFalse(one.catalogue_identification.compatible, kind)
                    self.assertFalse(two.catalogue_identification.compatible, kind)
                    self.assertFalse(two.cached, kind)
                    self.assertEqual(client.lookup_with_diagnostics.await_count, 2, kind)
                    self.assertEqual(test_db.get_cache_entries_count(), 0, kind)

        # 3) Finding 4: Refresh contradiction invalidates stale compatible cache (exact + alias)
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = TriageDB(Path(tmpdir) / "triage.db")
            test_db.init_db()
            req = EvaluateRequest(
                raw_text="Crystal.Harbor.2012.1080p.BluRay.REMUX-FIXTURE",
                hint_title="Crystal Harbor",
                hint_year=2012,
                torrent_info=TorrentInfo(imdb_id="tt1111111", tmdb_id=31, runtime_minutes=96),
            )
            alias_req = EvaluateRequest(
                raw_text="Le.Port.De.Cristal.2012.1080p.BluRay.REMUX-FIXTURE",
                hint_title="Le Port De Cristal",
                hint_year=2012,
                torrent_info=TorrentInfo(imdb_id="tt1111111", tmdb_id=31, runtime_minutes=96),
            )
            unrelated_2024_req = EvaluateRequest(
                raw_text="Crystal.Harbor.2024.1080p.BluRay.REMUX-FIXTURE",
                hint_title="Crystal Harbor",
                hint_year=2024,
                torrent_info=TorrentInfo(imdb_id="tt7777777", tmdb_id=95, runtime_minutes=125),
            )
            other = TmdbMovie(
                tmdb_id=95,
                imdb_id="tt1111111",
                canonical_title="Alternate Harbor",
                year=2024,
                director="Julie Quinn",
                runtime_minutes=125,
            )
            updated = False
            client = TmdbClient(api_key="invented")
            client._fetch_movie_details = lambda i: {31: original, 95: other if updated else remake}[i]  # type: ignore[method-assign]
            client._find_tmdb_id_by_imdb = lambda i: (95 if updated else 31) if i == "tt1111111" else 95  # type: ignore[method-assign]
            client._search_tmdb_movie = lambda *args, **kwargs: None  # type: ignore[method-assign]
            client.lookup_with_diagnostics = AsyncMock(wraps=client.lookup_with_diagnostics)  # type: ignore[method-assign]

            orig_eval = _sample_payload(
                title="Crystal Harbor",
                year=2012,
                director="Anna Linn",
                imdb_id="tt1111111",
                tmdb_id=31,
                runtime_minutes=96,
            )
            remake_eval = _sample_payload(
                title="Crystal Harbor",
                year=2024,
                director="Julie Quinn",
                imdb_id="tt7777777",
                tmdb_id=95,
                runtime_minutes=125,
            )

            with (
                patch.object(main, "db", test_db),
                patch.object(main, "tmdb_client", client),
                patch.object(
                    main.gemini_client,
                    "evaluate",
                    new=AsyncMock(side_effect=lambda payload, *a, **kw: remake_eval if payload.hint_year == 2024 else orig_eval),
                ),
            ):
                # Populate both the 2012 film and an unrelated 2024 film cleanly
                one = asyncio.run(main.evaluate(req))
                unrelated = asyncio.run(main.evaluate(unrelated_2024_req))
                self.assertFalse(one.cached)
                assert one.catalogue_identification is not None
                self.assertTrue(one.catalogue_identification.compatible)
                self.assertFalse(unrelated.cached)

                # Catalogue flips to contradiction; forced refresh must invalidate stale 2012 entry
                updated = True
                two = asyncio.run(main.evaluate(req.model_copy(update={"force_refresh": True})))
                three = asyncio.run(main.evaluate(req))
                four_alias = asyncio.run(main.evaluate(alias_req))
                # Unrelated 2024 entry stays intact and cached
                unrelated_again = asyncio.run(main.evaluate(unrelated_2024_req))

            assert two.catalogue_identification is not None
            assert three.catalogue_identification is not None
            assert four_alias.catalogue_identification is not None
            self.assertFalse(two.cached)
            self.assertFalse(two.catalogue_identification.compatible)
            self.assertFalse(three.cached)
            self.assertFalse(three.catalogue_identification.compatible)
            self.assertFalse(four_alias.cached)
            self.assertFalse(four_alias.catalogue_identification.compatible)
            self.assertTrue(unrelated_again.cached)

    def test_pr18_r4_finding_5_and_cache_regression_year_numeral_hint_and_disjoint_invalidation(self) -> None:
        """
        PR #18 Round 4 Finding 5 & Additional Cache Regression:
          - Title-ending numerals (e.g. 'Echo.2049.2017.1080p.BluRay.REMUX-Z' and
            'Wonder.Woman.1984.2020.2160p.UHD.BluRay.REMUX-Z') are parsed separately from the
            release year, allowing genuinely matching films to confirm and cache.
          - Contradictory upstream hints (e.g. 2012 filename with 2024 hint_year or unrelated
            hint_title) cannot mask filename evidence and force incompatible/uncached results.
          - Ordinary correct, localized alias, and RT rating attribution controls remain green.
          - Conflict invalidation removes only reachable entries for the conflicting release and
            its applicable IMDb aliases, preserving unrelated exact cache rows with disjoint IDs.
        """
        import main
        from google.genai import types as sdk
        from gemini_client import extract_grounding_sources, validate_and_ground_ratings
        from schemas import TorrentInfo
        from tmdb_client import extract_tmdb_query

        # 1) Title-ending numeral parsing & normalize_release_key
        self.assertEqual(extract_tmdb_query("Echo.2049.2017.1080p.BluRay.REMUX-Z"), ("Echo 2049", 2017))
        self.assertEqual(extract_tmdb_query("Wonder.Woman.1984.2020.2160p.UHD.BluRay.REMUX-Z"), ("Wonder Woman 1984", 2020))
        self.assertEqual(
            extract_tmdb_query("Moss.Field.2012.1080p.BluRay.REMUX-Z", hint_title="Moss Field", hint_year=2024),
            ("Moss Field", 2012),
        )

        # 2) Independent filename vs hint consistency across all controls and misleading-hint cases
        cases = (
            ("ordinary correct", "Moss.Field.2012.1080p.BluRay.REMUX-Z", "Moss Field", 2012, (), None, None, True),
            ("ordinary wrong", "Moss.Field.2012.1080p.BluRay.REMUX-Z", "Moss Field", 2024, (), None, None, False),
            ("valid localized alias", "Dolina.2012.1080p.BluRay.REMUX-Z", "Moss Field", 2012, ("Dolina",), None, None, True),
            ("correct title-ending numeral", "Echo.2049.2017.1080p.BluRay.REMUX-Z", "Echo 2049", 2017, (), None, None, True),
            ("misleading year hint", "Moss.Field.2012.1080p.BluRay.REMUX-Z", "Moss Field", 2024, (), "Moss Field", 2024, False),
            ("misleading title hint", "Moss.Field.2012.1080p.BluRay.REMUX-Z", "Other Film", 2012, (), "Other Film", 2012, False),
        )
        for label, raw, model_title, year, aliases, hint, hint_year, should_match in cases:
            with tempfile.TemporaryDirectory() as tmpdir:
                test_db = TriageDB(Path(tmpdir) / "state.db")
                test_db.init_db()
                req = EvaluateRequest(
                    raw_text=raw,
                    hint_title=hint,
                    hint_year=hint_year,
                    torrent_info=TorrentInfo(runtime_minutes=103),
                )
                ref = TmdbMovie(
                    tmdb_id=27,
                    imdb_id="tt1234567",
                    canonical_title=model_title,
                    year=year,
                    director="Dana Wren",
                    runtime_minutes=103,
                    alternative_titles=aliases,
                )
                client = TmdbClient(api_key="fixture")
                client._search_tmdb_movie = lambda *args, **kwargs: None  # type: ignore[method-assign]
                client._fetch_movie_details = lambda i: ref  # type: ignore[method-assign]
                client._find_tmdb_id_by_imdb = lambda i: 27  # type: ignore[method-assign]
                model_res = _sample_payload(
                    title=model_title,
                    year=year,
                    director="Dana Wren",
                    imdb_id="tt1234567",
                    tmdb_id=27,
                    runtime_minutes=103,
                )
                with (
                    patch.object(main, "db", test_db),
                    patch.object(main, "tmdb_client", client),
                    patch.object(main.gemini_client, "evaluate", new=AsyncMock(return_value=model_res)),
                ):
                    first = asyncio.run(main.evaluate(req))
                    second = asyncio.run(main.evaluate(req))
                assert first.catalogue_identification is not None
                self.assertEqual(first.catalogue_identification.compatible, should_match, label)
                self.assertEqual(second.cached, should_match, label)

        # 3) RT rating attribution positive & negative controls
        web = {"uri": "https://www.rottentomatoes.com/m/moss_field_2012", "title": "Moss Field (2012)"}
        seg = "Moss Field: 82% Tomatometer from 74 critic reviews; audience score 89%."
        resp = sdk.GenerateContentResponse(
            candidates=[
                {
                    "grounding_metadata": {
                        "grounding_chunks": [{"web": web}],
                        "grounding_supports": [{"grounding_chunk_indices": [0], "segment": {"text": seg}}],
                    }
                }
            ]
        )
        sources = extract_grounding_sources(resp)
        good_rt = validate_and_ground_ratings(
            {"rotten_tomatoes_critics": 82, "rotten_tomatoes_audience": 89},
            grounding_sources=sources,
            identification={"canonical_title": "Moss Field", "year": 2012},
        )
        self.assertEqual(good_rt["rotten_tomatoes_critics"], 82)
        self.assertEqual(good_rt["rotten_tomatoes_audience"], 89)
        bad_rt = validate_and_ground_ratings(
            {"rotten_tomatoes_critics": 74, "rotten_tomatoes_audience": 82},
            grounding_sources=sources,
            identification={"canonical_title": "Moss Field", "year": 2012},
        )
        self.assertIsNone(bad_rt["rotten_tomatoes_critics"])
        self.assertIsNone(bad_rt["rotten_tomatoes_audience"])

        # 4) Additional cache regression: invalidating one release preserves disjoint-ID exact cache row
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = TriageDB(Path(tmpdir) / "cache.db")
            test_db.init_db()
            rel_a = "Harbor.2013.German.1080p.BluRay.REMUX-GRP"
            rel_b = "Harbor.2013.French.1080p.BluRay.REMUX-GRP"
            test_db.save_evaluation(
                rel_a,
                _sample_payload(title="Harbor", year=2013, imdb_id="tt1111111", tmdb_id=1).model_dump(),
            )
            test_db.save_evaluation(
                rel_b,
                _sample_payload(title="Harbor", year=2013, imdb_id="tt2222222", tmdb_id=2).model_dump(),
            )
            self.assertIsNotNone(
                test_db.get_cached_evaluation(rel_a, allow_imdb_lookup=False, allow_release_key_fallback=False)
            )
            self.assertIsNotNone(
                test_db.get_cached_evaluation(rel_b, allow_imdb_lookup=False, allow_release_key_fallback=False)
            )
            test_db.invalidate_evaluations(rel_a, imdb_ids=["tt1111111"])
            self.assertIsNone(
                test_db.get_cached_evaluation(rel_a, allow_imdb_lookup=False, allow_release_key_fallback=False)
            )
            self.assertIsNotNone(
                test_db.get_cached_evaluation(rel_b, allow_imdb_lookup=False, allow_release_key_fallback=False)
            )

    def test_pr18_r5_findings_1_and_4_search_conflict_and_dated_restoration_year(self) -> None:
        """
        PR #18 Round 5 Findings 1 & 4 (Triage):
          - Finding 1: Title search MUST apply the same release-evidence check before accepting
            or caching a candidate, so returning the exact catalogue record rejected for a
            40-minute runtime mismatch stays incompatible and uncached, while a genuinely
            different matching record resolves the mistaken tracker ID cleanly.
          - Finding 4: Consecutive film year + labeled restoration/remaster year (e.g.
            'Granite.Bay.1994.2022.Restoration.1080p.BluRay.REMUX-Z') preserves 1994 as the
            release year in both extract_tmdb_query and normalize_release_key, while real
            numeral-ending titles ('Road.2049.2017') continue to parse cleanly.
        """
        import main
        from db import normalize_release_key
        from schemas import TorrentInfo
        from tmdb_client import extract_tmdb_query

        # 1) Finding 1: Search cannot re-approve a record rejected for runtime conflict
        film_135 = {
            "id": 59,
            "title": "North Window",
            "release_date": "2012-04-09",
            "runtime": 135,
            "external_ids": {"imdb_id": "tt5550001"},
        }
        film_95 = {
            "id": 60,
            "title": "North Window",
            "release_date": "2012-04-09",
            "runtime": 95,
            "external_ids": {"imdb_id": "tt5550002"},
        }
        search_cases = (
            ("consistent", 135, [film_135], (True, True), film_135),
            ("conflict_without_search_result", 95, [], (False, False), film_135),
            ("same_rejected_record_in_search", 95, [film_135], (False, False), film_135),
            ("genuinely_different_matching_search", 95, [film_135, film_95], (True, True), film_95),
        )
        for label, measured, search_results, expected, model_film in search_cases:
            with tempfile.TemporaryDirectory() as tmpdir:
                test_db = TriageDB(Path(tmpdir) / f"{label}.db")
                test_db.init_db()
                client = TmdbClient(api_key="fixture")

                def _catalogue(path: str, params=None, _sr=search_results):
                    if path == "/movie/59":
                        return film_135
                    if path == "/movie/60":
                        return film_95
                    if path == "/search/movie":
                        return {"results": _sr}
                    if path == "/find/tt5550001":
                        return {"movie_results": [{"id": 59}]}
                    if path == "/find/tt5550002":
                        return {"movie_results": [{"id": 60}]}
                    raise AssertionError(path)

                client._get_json = _catalogue  # type: ignore[method-assign]
                model_res = _sample_payload(
                    title=model_film["title"],
                    year=2012,
                    runtime_minutes=model_film["runtime"],
                    imdb_id=model_film["external_ids"]["imdb_id"],
                    tmdb_id=model_film["id"],
                )
                req = EvaluateRequest(
                    raw_text="North.Window.2012.1080p.BluRay.REMUX-GRP",
                    hint_title="North Window",
                    hint_year=2012,
                    torrent_info=TorrentInfo(tmdb_id=59, runtime_minutes=measured),
                )
                with (
                    patch.object(main, "db", test_db),
                    patch.object(main, "tmdb_client", client),
                    patch.object(main.gemini_client, "evaluate", new=AsyncMock(return_value=model_res)),
                ):
                    first = asyncio.run(main.evaluate(req))
                    second = asyncio.run(main.evaluate(req))
                assert first.catalogue_identification is not None
                self.assertEqual((first.catalogue_identification.compatible, second.cached), expected, label)

        # 2) Finding 4: Labeled restoration years vs title-ending numerals (query + cache key parity)
        parse_checks = (
            ("Granite.Bay.1994.1080p.BluRay.REMUX-Z", ("Granite Bay", 1994), "granite bay 1994"),
            ("Road.2049.2017.1080p.BluRay.REMUX-Z", ("Road 2049", 2017), "road 2049 2017"),
            ("Granite.Bay.1994.2022.Restoration.1080p.BluRay.REMUX-Z", ("Granite Bay", 1994), "granite bay 1994"),
            ("Granite.Bay.1994.2022.4K.Restoration.1080p.BluRay.REMUX-Z", ("Granite Bay", 1994), "granite bay 1994"),
            ("Road.2049.2017.2024.Restoration.1080p.BluRay.REMUX-Z", ("Road 2049", 2017), "road 2049 2017"),
            ("Wonder.Woman.1984.2020.Extended.Edition.2160p.UHD.BluRay.REMUX-Z", ("Wonder Woman 1984", 2020), "wonder woman 1984 2020"),
        )
        for raw, exp_query, exp_rel_key in parse_checks:
            self.assertEqual(extract_tmdb_query(raw), exp_query, raw)
            self.assertEqual(normalize_release_key(raw), exp_rel_key, raw)

        for label, raw, title, year in (
            ("normal match", "Granite.Bay.1994.1080p.BluRay.REMUX-Z", "Granite Bay", 1994),
            ("clear numeric title", "Road.2049.2017.1080p.BluRay.REMUX-Z", "Road 2049", 2017),
            ("dated restoration", "Granite.Bay.1994.2022.Restoration.1080p.BluRay.REMUX-Z", "Granite Bay", 1994),
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                test_db = TriageDB(Path(tmpdir) / "db.sqlite")
                test_db.init_db()
                movie = TmdbMovie(
                    tmdb_id=33,
                    imdb_id="tt1111111",
                    canonical_title=title,
                    year=year,
                    director="Jane Cole",
                    runtime_minutes=103,
                )
                client = TmdbClient(api_key="fixture")
                client._search_results = lambda *a, _t=title, _y=year, **k: [  # type: ignore[method-assign]
                    {"id": 33, "title": _t, "original_title": _t, "release_date": f"{_y}-09-15", "popularity": 10}
                ]
                client._fetch_movie_details = lambda i, _m=movie: _m  # type: ignore[method-assign]
                client._find_tmdb_id_by_imdb = lambda i: 33  # type: ignore[method-assign]
                req = EvaluateRequest(
                    raw_text=raw,
                    hint_title=title,
                    hint_year=year,
                    torrent_info=TorrentInfo(runtime_minutes=103),
                )
                model_res = _sample_payload(
                    title=title,
                    year=year,
                    director="Jane Cole",
                    runtime_minutes=103,
                    imdb_id="tt1111111",
                    tmdb_id=33,
                )
                with (
                    patch.object(main, "db", test_db),
                    patch.object(main, "tmdb_client", client),
                    patch.object(main.gemini_client, "evaluate", new=AsyncMock(return_value=model_res)),
                ):
                    first = asyncio.run(main.evaluate(req))
                    second = asyncio.run(main.evaluate(req))
                assert first.catalogue_identification is not None
                self.assertTrue(first.catalogue_identification.compatible, label)
                self.assertTrue(second.cached, label)

    def test_pr18_r6_finding_2_chronology_and_corroborating_consecutive_years(self) -> None:
        """
        PR #18 Round 6 Finding 2 (Triage):
          - Chronology: a remaster/restoration label after Y2 MUST NOT treat Y1 as the release
            year when Y2 <= Y1 (e.g. 'Crew.Of.1999.1990.Remastered' -> ('Crew Of 1999', 1990),
            'Crew.Of.1984.1984.Remastered' -> ('Crew Of 1984', 1984)).
          - Ambiguous chronological pairs (Y1 < Y2 + edition marker, e.g. '1984.2020.Remastered'
            vs '1994.2022.Restoration') resolve via corroborating title/year hints or catalogue
            search/evidence while rejecting misleading hints and wrong-film records.
          - All prior controls (normal match, Road 2049, Granite Bay 1994 2022 Restoration) stay green.
        """
        import main
        from db import normalize_release_key
        from schemas import TorrentInfo
        from tmdb_client import extract_filename_tmdb_query, extract_tmdb_query

        # 1) Pure filename parsing & cache-key parity (no hints)
        no_hint_checks = (
            ("Granite.Bay.1994.1080p.BluRay.REMUX-Z", ("Granite Bay", 1994), "granite bay 1994"),
            ("Road.2049.2017.1080p.BluRay.REMUX-Z", ("Road 2049", 2017), "road 2049 2017"),
            ("Granite.Bay.1994.2022.Restoration.1080p.BluRay.REMUX-Z", ("Granite Bay", 1994), "granite bay 1994"),
            ("Crew.Of.1999.1990.Remastered.1080p.BluRay.REMUX-Z", ("Crew Of 1999", 1990), "crew of 1999 1990"),
            ("Crew.Of.1984.1984.Remastered.1080p.BluRay.REMUX-Z", ("Crew Of 1984", 1984), "crew of 1984 1984"),
            ("Crew.Of.1999.1990.2022.Restoration.1080p.BluRay.REMUX-Z", ("Crew Of 1999", 1990), "crew of 1999 1990"),
        )
        for raw, exp_query, exp_key in no_hint_checks:
            self.assertEqual(extract_filename_tmdb_query(raw), exp_query, raw)
            self.assertEqual(extract_tmdb_query(raw), exp_query, raw)
            self.assertEqual(normalize_release_key(raw), exp_key, raw)

        # 2) Corroborating hints resolve chronologically ambiguous Y1 < Y2 + Remastered,
        #    while contradictory hints cannot hide a release-year conflict
        self.assertEqual(
            extract_filename_tmdb_query(
                "Wonder.Woman.1984.2020.Remastered.2160p.UHD.BluRay.REMUX-Z",
                hint_title="Wonder Woman 1984",
                hint_year=2020,
            ),
            ("Wonder Woman 1984", 2020),
        )
        self.assertEqual(
            normalize_release_key(
                "Wonder.Woman.1984.2020.Remastered.2160p.UHD.BluRay.REMUX-Z",
                hint_title="Wonder Woman 1984",
                hint_year=2020,
            ),
            "wonder woman 1984 2020",
        )
        self.assertEqual(
            extract_filename_tmdb_query(
                "Granite.Bay.1994.2022.Restoration.1080p.BluRay.REMUX-Z",
                hint_title="Granite Bay",
                hint_year=2022,
            ),
            ("Granite Bay", 1994),
        )
        # Never flip to title-numeral reading on hint_year alone when hint_title omits the numeral
        for misleading_title in (None, "Baie de Granit", "Granite Bay: 4K Restoration"):
            self.assertEqual(
                extract_filename_tmdb_query(
                    "Granite.Bay.1994.2022.Restoration.1080p.BluRay.REMUX-Z",
                    hint_title=misleading_title,
                    hint_year=2022,
                ),
                ("Granite Bay", 1994),
                str(misleading_title),
            )
        # Localized title carrying the numeral + matching year still corroborates the numeral title
        self.assertEqual(
            extract_filename_tmdb_query(
                "Wonder.Woman.1984.2020.Remastered.2160p.UHD.BluRay.REMUX-Z",
                hint_title="Mujer Maravilla 1984",
                hint_year=2020,
            ),
            ("Wonder Woman 1984", 2020),
        )

        # 3) End-to-end evaluate checks (positive controls + Round 6 fix + negative controls)
        eval_cases = (
            ("normal match", "Granite.Bay.1994.1080p.BluRay.REMUX-Z", "Granite Bay", 1994, "Granite Bay", 1994, True),
            ("clear numeric title", "Road.2049.2017.1080p.BluRay.REMUX-Z", "Road 2049", 2017, "Road 2049", 2017, True),
            ("dated restoration", "Granite.Bay.1994.2022.Restoration.1080p.BluRay.REMUX-Z", "Granite Bay", 1994, "Granite Bay", 1994, True),
            ("past numeral title on remastered disc", "Crew.Of.1999.1990.Remastered.1080p.BluRay.REMUX-Z", "Crew of 1999", 1990, "Crew of 1999", 1990, True),
            ("past numeral title remastered no hints", "Crew.Of.1999.1990.Remastered.1080p.BluRay.REMUX-Z", "Crew of 1999", 1990, None, None, True),
            ("earlier numeral title remastered with hints", "Wonder.Woman.1984.2020.Remastered.2160p.UHD.BluRay.REMUX-Z", "Wonder Woman 1984", 2020, "Wonder Woman 1984", 2020, True),
            ("earlier numeral title remastered no hints", "Wonder.Woman.1984.2020.Remastered.2160p.UHD.BluRay.REMUX-Z", "Wonder Woman 1984", 2020, None, None, True),
            ("earlier numeral title remastered year-only hint", "Wonder.Woman.1984.2020.Remastered.2160p.UHD.BluRay.REMUX-Z", "Wonder Woman 1984", 2020, None, 2020, True),
            ("reject impossible chronology film", "Crew.Of.1999.1990.Remastered.1080p.BluRay.REMUX-Z", "Crew Of", 1999, None, None, False),
            ("reject misleading restoration year hint", "Granite.Bay.1994.2022.Restoration.1080p.BluRay.REMUX-Z", "Granite Bay", 2022, "Granite Bay", 2022, False),
        )
        for label, raw, movie_title, movie_year, hint_title, hint_year, should_match in eval_cases:
            with tempfile.TemporaryDirectory() as tmpdir:
                test_db = TriageDB(Path(tmpdir) / "db.sqlite")
                test_db.init_db()
                movie = TmdbMovie(
                    tmdb_id=33,
                    imdb_id="tt1111111",
                    canonical_title=movie_title,
                    year=movie_year,
                    director="Jane Cole",
                    runtime_minutes=103,
                )
                client = TmdbClient(api_key="fixture")

                def _search(q_title: str, q_year: Optional[int] = None, _t=movie_title, _y=movie_year):
                    if _t.lower() == q_title.lower() and (q_year is None or abs(q_year - _y) <= 1):
                        return [{"id": 33, "title": _t, "original_title": _t, "release_date": f"{_y}-09-15", "popularity": 10}]
                    return []

                client._search_results = _search  # type: ignore[method-assign]
                client._fetch_movie_details = lambda i, _m=movie: _m  # type: ignore[method-assign]
                client._find_tmdb_id_by_imdb = lambda i: 33  # type: ignore[method-assign]
                req = EvaluateRequest(
                    raw_text=raw,
                    hint_title=hint_title,
                    hint_year=hint_year,
                    torrent_info=TorrentInfo(runtime_minutes=103),
                )
                model_res = _sample_payload(
                    title=movie_title,
                    year=movie_year,
                    director="Jane Cole",
                    runtime_minutes=103,
                    imdb_id="tt1111111",
                    tmdb_id=33,
                )
                with (
                    patch.object(main, "db", test_db),
                    patch.object(main, "tmdb_client", client),
                    patch.object(main.gemini_client, "evaluate", new=AsyncMock(return_value=model_res)),
                ):
                    first = asyncio.run(main.evaluate(req))
                    second = asyncio.run(main.evaluate(req))
                assert first.catalogue_identification is not None
                self.assertEqual(first.catalogue_identification.compatible, should_match, label)
                self.assertEqual(second.cached, should_match, label)

        # 4) Non-Latin film from the restoration year MUST NOT bypass title-numeral corroboration
        #    via non_latin_corroborated (whether with no hints, year-only hint, or localized alias)
        for label, hint_t, hint_y in (
            ("non-latin 2022 remake no hints", None, None),
            ("non-latin 2022 remake year-only hint", None, 2022),
            ("non-latin 2022 remake localized alias + misleading year", "Baie de Granit", 2022),
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                test_db = TriageDB(Path(tmpdir) / "db.sqlite")
                test_db.init_db()
                remake_2022 = TmdbMovie(
                    tmdb_id=99,
                    imdb_id="tt9999999",
                    canonical_title="Granite Bay",
                    original_title="花崗岩湾",
                    year=2022,
                    director="Wrong Dir",
                    runtime_minutes=103,
                    original_language="ja",
                )
                client = TmdbClient(api_key="fixture")
                client._search_results = lambda *a, **k: [  # type: ignore[method-assign]
                    {
                        "id": 99,
                        "title": "Granite Bay",
                        "original_title": "花崗岩湾",
                        "release_date": "2022-05-01",
                        "popularity": 10,
                    }
                ]
                client._fetch_movie_details = lambda i, _m=remake_2022: _m  # type: ignore[method-assign]
                client._find_tmdb_id_by_imdb = lambda i: 99  # type: ignore[method-assign]
                req = EvaluateRequest(
                    raw_text="Granite.Bay.1994.2022.Restoration.1080p.BluRay.REMUX-Z",
                    hint_title=hint_t,
                    hint_year=hint_y,
                    torrent_info=TorrentInfo(tmdb_id=99, runtime_minutes=103, audio_languages=["Japanese"]),
                )
                model_res = _sample_payload(
                    title="Granite Bay",
                    year=2022,
                    director="Wrong Dir",
                    runtime_minutes=103,
                    imdb_id="tt9999999",
                    tmdb_id=99,
                )
                with (
                    patch.object(main, "db", test_db),
                    patch.object(main, "tmdb_client", client),
                    patch.object(main.gemini_client, "evaluate", new=AsyncMock(return_value=model_res)),
                ):
                    first = asyncio.run(main.evaluate(req))
                    second = asyncio.run(main.evaluate(req))
                assert first.catalogue_identification is not None
                self.assertFalse(first.catalogue_identification.compatible, label)
                self.assertFalse(second.cached, label)

        # 5) Localized alias omitting the year numeral STILL resolves a genuine remastered
        #    numeral-title film when catalogue titles corroborate both numeral and alias
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = TriageDB(Path(tmpdir) / "db.sqlite")
            test_db.init_db()
            echo_1984 = TmdbMovie(
                tmdb_id=44,
                imdb_id="tt4444444",
                canonical_title="Echo 1984",
                year=2020,
                director="Jane Cole",
                runtime_minutes=103,
                alternative_titles=("Dolina",),
            )
            client = TmdbClient(api_key="fixture")

            def _search_echo(q_title: str, q_year: Optional[int] = None):
                if q_title.lower() == "echo 1984" and (q_year is None or abs(q_year - 2020) <= 1):
                    return [{"id": 44, "title": "Echo 1984", "original_title": "Echo 1984", "release_date": "2020-09-15", "popularity": 10}]
                return []

            client._search_results = _search_echo  # type: ignore[method-assign]
            client._fetch_movie_details = lambda i: echo_1984  # type: ignore[method-assign]
            client._find_tmdb_id_by_imdb = lambda i: 44  # type: ignore[method-assign]
            req = EvaluateRequest(
                raw_text="Echo.1984.2020.Remastered.1080p.BluRay.REMUX-Z",
                hint_title="Dolina",
                hint_year=2020,
                torrent_info=TorrentInfo(runtime_minutes=103),
            )
            model_res = _sample_payload(
                title="Echo 1984",
                year=2020,
                director="Jane Cole",
                runtime_minutes=103,
                imdb_id="tt4444444",
                tmdb_id=44,
            )
            with (
                patch.object(main, "db", test_db),
                patch.object(main, "tmdb_client", client),
                patch.object(main.gemini_client, "evaluate", new=AsyncMock(return_value=model_res)),
            ):
                first = asyncio.run(main.evaluate(req))
                second = asyncio.run(main.evaluate(req))
            assert first.catalogue_identification is not None
            self.assertTrue(first.catalogue_identification.compatible)
            self.assertTrue(second.cached)

    def test_pr18_r7_finding_2_ambiguous_dual_catalogue_films_withhold_trust(self) -> None:
        """
        PR #18 Round 7 Finding 2 (Triage):
          When an ambiguous consecutive-year remastered filename (`Granite.Bay.1994.2022.Remastered`)
          matches TWO distinct catalogue films across its plausible readings (`Granite Bay` (1994)
          and `Granite Bay 1994` (2022)) with no deciding hint, runtime, or explicit ID, Triage MUST
          check all plausible readings, report an ambiguity conflict, and withhold caching, while
          continuing to accept single-film catalogues and evidence-resolved disambiguations.
        """
        import main
        from schemas import TorrentInfo

        def _film(
            identifier: int,
            title: str,
            year: int,
            *,
            runtime: int = 103,
            japanese: bool = False,
        ) -> TmdbMovie:
            return TmdbMovie(
                tmdb_id=identifier,
                imdb_id=f"tt{identifier:07d}",
                canonical_title=title,
                year=year,
                director="Jane Cole",
                runtime_minutes=runtime,
                original_title="花崗岩湾" if japanese else title,
                original_language="ja" if japanese else "en",
            )

        original = _film(31, "Granite Bay", 1994)
        numbered = _film(32, "Granite Bay 1994", 2022)
        numbered_long = _film(34, "Granite Bay 1994", 2022, runtime=145)
        remake = _film(33, "Granite Bay", 2022, japanese=True)
        release = "Granite.Bay.1994.2022.Remastered.1080p.BluRay.REMUX-Z"

        def _assess(
            label: str,
            catalogue: list[TmdbMovie],
            model_pick: TmdbMovie,
            expected: bool,
            *,
            hint: Optional[str] = None,
            year: Optional[int] = None,
            tracker: Optional[int] = None,
        ) -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                test_db = TriageDB(Path(tmpdir) / "triage.db")
                test_db.init_db()
                client = TmdbClient(api_key="fixture")
                by_id = {m.tmdb_id: m for m in catalogue}

                def _search(q_title: str, q_year: Optional[int] = None):
                    return [
                        {
                            "id": m.tmdb_id,
                            "title": m.canonical_title,
                            "original_title": m.original_title,
                            "release_date": f"{m.year}-05-05",
                            "popularity": 10,
                        }
                        for m in catalogue
                        if m.canonical_title.lower() == q_title.lower() and m.year == q_year
                    ]

                client._search_results = _search  # type: ignore[method-assign]
                client._fetch_movie_details = by_id.__getitem__  # type: ignore[method-assign]
                client._find_tmdb_id_by_imdb = lambda imdb: next(  # type: ignore[method-assign]
                    (m.tmdb_id for m in catalogue if m.imdb_id == imdb),
                    None,
                )
                req = EvaluateRequest(
                    raw_text=release,
                    hint_title=hint,
                    hint_year=year,
                    torrent_info=TorrentInfo(runtime_minutes=103, tmdb_id=tracker),
                )
                model = AsyncMock(
                    return_value=_sample_payload(
                        title=model_pick.canonical_title,
                        year=model_pick.year,
                        director=model_pick.director or "Jane Cole",
                        runtime_minutes=103,
                        imdb_id=model_pick.imdb_id or "tt0000031",
                        tmdb_id=model_pick.tmdb_id,
                    )
                )
                with (
                    patch.object(main, "db", test_db),
                    patch.object(main, "tmdb_client", client),
                    patch.object(main.gemini_client, "evaluate", model),
                ):
                    first = asyncio.run(main.evaluate(req))
                    repeat = asyncio.run(main.evaluate(req))
                assert first.catalogue_identification is not None
                self.assertEqual(first.catalogue_identification.compatible, expected, label)
                self.assertEqual(repeat.cached, expected, label)

        _assess("only original", [original], original, True)
        _assess("only numeral title", [numbered], numbered, True)
        _assess("two distinct matching films; no deciding evidence", [original, numbered], original, False)
        _assess("two films, explicit ID selects numeral title", [original, numbered], numbered, True, tracker=numbered.tmdb_id)
        _assess("two films, explicit hint selects original", [original, numbered], original, True, hint="Granite Bay", year=1994)
        _assess("two films, explicit hint selects numeral title", [original, numbered], numbered, True, hint="Granite Bay 1994", year=2022)
        _assess("two films, distinguishing runtime selects original", [original, numbered_long], original, True)
        _assess("remake without numeral, year only", [remake], remake, False, year=2022, tracker=remake.tmdb_id)
        _assess("same remake, parsed title hint contains original year", [remake], remake, False, hint="Granite Bay 1994", year=2022, tracker=remake.tmdb_id)


if __name__ == "__main__":
    unittest.main()
