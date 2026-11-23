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
            db.save_evaluation("Anatomie.d.une.chute.2023.FRENCH.2160p", payload)

            # Different language title hits cache when TMDB resolves the IMDb ID
            hit = db.get_cached_evaluation(
                "Anatomy.of.a.Fall.2023.1080p.BluRay.x264-GRP",
                hint_title="Anatomy of a Fall",
                hint_year=2023,
                extra_context="tt17009710",
            )
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertEqual(hit["identification"]["tmdb_id"], 915935)
            self.assertEqual(hit["identification"]["imdb_id"], "tt17009710")

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
            mock_tmdb = TmdbClient(api_key="dummy-key")
            mock_tmdb.lookup_from_request = AsyncMock(return_value=tmdb_hit)  # type: ignore[method-assign]
            mock_tmdb.verify_identification = AsyncMock(return_value=None)  # type: ignore[method-assign]
            mock_gemini_eval = AsyncMock(return_value=gemini_raw)

            with (
                patch.object(main, "db", test_db),
                patch.object(main, "tmdb_client", mock_tmdb),
                patch.object(main.gemini_client, "evaluate", mock_gemini_eval),
            ):
                # 1) Uncached evaluation gets TMDB grounding + deterministic enrichment
                resp1 = asyncio.run(
                    main.evaluate(
                        EvaluateRequest(raw_text="La.Chimera.2023.1080p.BluRay.x264-GROUP")
                    )
                )
                self.assertFalse(resp1.cached)
                self.assertEqual(resp1.identification.tmdb_id, 837335)
                self.assertEqual(resp1.identification.imdb_id, "tt14561712")
                self.assertEqual(resp1.identification.runtime_minutes, 133)
                self.assertEqual(resp1.identification.original_title, "La chimera")
                self.assertEqual(mock_gemini_eval.await_count, 1)
                passed_ctx = mock_gemini_eval.await_args.kwargs["tmdb_context"]
                self.assertIn("TMDB ID: 837335", passed_ctx)

                # 2) Alias release string resolves via TMDB -> IMDb ID -> cache hit (no 2nd Gemini call)
                resp2 = asyncio.run(
                    main.evaluate(
                        EvaluateRequest(raw_text="The.Chimera.2023.2160p.REMUX-OTHER")
                    )
                )
                self.assertTrue(resp2.cached)
                self.assertEqual(resp2.identification.tmdb_id, 837335)
                self.assertEqual(mock_gemini_eval.await_count, 1)

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


if __name__ == "__main__":
    unittest.main()
