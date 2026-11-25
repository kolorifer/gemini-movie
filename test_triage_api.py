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

            # Backward-compatible mode when INTERNAL_API_SECRET is empty
            with (
                patch.object(main, "db", test_db),
                patch.object(main, "INTERNAL_API_SECRET", ""),
                TestClient(main.app) as client,
            ):
                health_open = client.get("/health")
                self.assertEqual(health_open.status_code, 200)
                self.assertFalse(health_open.json()["auth_enabled"])
                self.assertEqual(client.get("/recent").status_code, 200)

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
                self.assertEqual(mock_gemini_eval.await_count, 1)

                # 2) Same 2025 release with new MediaInfo/tracker evidence MUST miss cache
                tinfo_a = TorrentInfo(
                    indexer="TorrentLeech",
                    imdb_id="tt0088117",
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

            db = TriageDB(db_file)
            db.init_db()

            # Directory is tightened to 0700 and DB file to 0600
            self.assertEqual(stat.S_IMODE(data_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(db_file.stat().st_mode), 0o600)

            # Migrated row is readable with empty evidence_key, and blocked across remake years
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


if __name__ == "__main__":
    unittest.main()
