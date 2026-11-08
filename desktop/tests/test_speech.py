"""Voice: joining packets, decoding, the WAV, the speech-to-text call and every way a clip can go wrong."""
import base64
import struct
import unittest

import helpers
from helpers import SPEECH_KEY, TRANSCRIPT_MARK, Clock, FakeSpeech, FakeToggl, HomeCase, audio_messages, connect, disconnect, make_backend, speech_pcm

from secrets_store import MemoryStore
from speech import adpcm, audio, clips, stt
from speech.stt import SpeechClient, cut, match_project, tidy


class Audio(unittest.TestCase):
    def test_the_wav_header(self):
        pcm = [0, 1000, -1000, 32767, -32768]
        wav = audio.wav_bytes(pcm, 16000)
        self.assertEqual(len(wav), 44 + 2 * len(pcm))
        fields = struct.unpack("<4sI4s4sIHHIIHH4sI", wav[:44])
        self.assertEqual(fields, (b"RIFF", 36 + 10, b"WAVE", b"fmt ", 16, 1, 1, 16000, 32000, 2, 16, b"data", 10))
        self.assertEqual(struct.unpack("<5h", wav[44:]), tuple(pcm))

    def test_the_standard_library_can_read_it(self):
        import io
        import wave
        with wave.open(io.BytesIO(audio.wav_bytes([1, 2, 3, 4], 16000))) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()), (1, 2, 16000, 4))

    def test_silence_and_short_clips_hold_no_speech(self):
        self.assertIsNone(audio.prepare([0] * 16000, 16000))
        self.assertIsNone(audio.prepare([5000] * 100, 16000))
        self.assertIsNone(audio.prepare([], 16000))

    def test_speech_is_trimmed_and_normalized(self):
        pcm = speech_pcm(2.0, amplitude=2000)
        out = audio.prepare(pcm, 16000)
        self.assertLess(len(out), len(pcm))             # the quiet ends are gone...
        self.assertGreater(len(out), len(pcm) // 2)     # ...but the loud middle stays, with some room around it
        self.assertGreater(max(abs(v) for v in out), 15000)
        self.assertLessEqual(max(abs(v) for v in out), 32767)

    def test_a_packet_decodes_on_its_own_state(self):
        pcm = speech_pcm(0.5)
        packets = audio_messages(pcm, samples_per_packet=1000)
        whole = []
        for p in packets:
            whole += adpcm.decode(base64.b64decode(p["b64"]), p["n"], p["p"], p["i"])
        self.assertEqual(len(whole), len(pcm))
        last = packets[-1]
        self.assertEqual(adpcm.decode(base64.b64decode(last["b64"]), last["n"], last["p"], last["i"]), whole[-last["n"]:])
        self.assertLess(sum(abs(a - b) for a, b in zip(whole, pcm)) / len(pcm), 500)  # close to what went in

    def test_decode_packets_needs_every_packet(self):
        good = {0: (0, 0, 2, b"\x11"), 1: (0, 0, 2, b"\x11")}
        self.assertEqual(len(clips.decode_packets(good, 2)), 4)
        self.assertIsNone(clips.decode_packets(good, 3))
        self.assertIsNone(clips.decode_packets({0: good[0]}, 2))
        self.assertIsNone(clips.decode_packets({0: good[0], 2: good[1]}, 2))
        self.assertIsNone(clips.decode_packets({0: (0, 0, 5, b"\x11")}, 1))  # says 5 samples, carries 2


class Text(unittest.TestCase):
    def test_tidy(self):
        self.assertEqual(tidy("  Acme   design\nreview. "), "Acme design review")
        self.assertEqual(tidy("..."), "")
        self.assertEqual(tidy(None), "")

    def test_cut_never_splits_a_character(self):
        self.assertEqual(cut("é" * 60), "é" * 47)
        self.assertLessEqual(len(cut("€" * 60).encode()), 95)
        self.assertEqual(cut("short"), "short")

    def test_the_project_match_is_whole_words_ignoring_case(self):
        projects = [{"id": 1, "name": "Acme"}, {"id": 2, "name": "Acme Design"}, {"id": 3, "name": "Art"}, {"id": 4, "name": ""}]
        self.assertEqual(match_project("acme design review", projects), 2)    # the longest wins
        self.assertEqual(match_project("Call ACME today", projects), 1)
        self.assertEqual(match_project("Smart ideas", projects), 0)           # "art" inside a word doesn't count
        self.assertEqual(match_project("art, then acmes", projects), 3)
        self.assertEqual(match_project("smart art", projects), 3)             # a later whole word counts
        self.assertEqual(match_project("nothing", []), 0)

    def test_the_match_knows_more_than_ascii(self):
        self.assertEqual(match_project("déjà vu", [{"id": 5, "name": "Vu"}]), 5)
        self.assertEqual(match_project("évu", [{"id": 5, "name": "vu"}]), 0)


class Service(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSpeech()
        self.addCleanup(self.fake.close)
        self.key = SPEECH_KEY
        self.client = SpeechClient(self.fake.url, lambda: self.key)
        self.wav = audio.wav_bytes([1, 2, 3, 4], 16000)

    def test_the_request_is_the_one_cloud_c_makes(self):
        self.assertEqual(self.client.transcribe(self.wav, ["Deep work", "Acme"]), "Acme design review")
        r = self.fake.requests[0]
        self.assertEqual(r["auth"], f"Bearer {SPEECH_KEY}")
        self.assertEqual(r["model"], "gpt-transcribe")
        self.assertEqual(r["response_format"], "json")
        self.assertEqual(r["keywords"], ["Deep work", "Acme"])
        self.assertEqual((r["filename"], r["file_type"]), ("task.wav", "audio/wav"))
        self.assertEqual(r["file"], self.wav)

    def test_the_default_address_is_the_real_service(self):
        with open(helpers.DESKTOP + "/config.py") as f:
            self.assertIn('"https://api.openai.com/v1/audio/transcriptions"', f.read())

    def test_keywords_turned_down_are_retried_without_and_not_sent_again(self):
        self.fake.script.append(400)
        self.assertEqual(self.client.transcribe(self.wav, ["Acme"]), "Acme design review")
        self.assertEqual([r["keywords"] for r in self.fake.requests], [["Acme"], []])
        self.client.transcribe(self.wav, ["Acme"])
        self.assertEqual(self.fake.requests[2]["keywords"], [])

    def test_a_dropped_connection_gets_one_more_try(self):
        self.fake.script.append(None)
        self.assertEqual(self.client.transcribe(self.wav), "Acme design review")
        self.assertEqual(len(self.fake.requests), 2)
        self.fake.script.extend([None, None])
        self.assertIsNone(self.client.transcribe(self.wav))
        self.assertEqual(len(self.fake.requests), 4)

    def test_a_refusal_is_a_failure(self):
        self.fake.script.append(500)
        self.assertIsNone(self.client.transcribe(self.wav))
        self.key = "wrong"
        self.assertIsNone(self.client.transcribe(self.wav))

    def test_no_key_makes_no_request(self):
        self.key = None
        self.assertIsNone(self.client.transcribe(self.wav))
        self.assertEqual(self.fake.requests, [])

    def test_very_many_keywords_stay_within_what_cloud_c_sent(self):
        self.client.transcribe(self.wav, [f"Project number {i}" for i in range(1000)])
        sent = self.fake.requests[0]["keywords"]
        self.assertGreater(len(sent), 5)
        self.assertLess(sum(len(k) for k in sent), stt.KEYWORD_ROOM)


class Clips(HomeCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeSpeech()
        self.addCleanup(self.fake.close)
        self.backend = make_backend(speech=self.fake)
        self.backend.runs.projects = [{"id": 123, "name": "Acme", "color": "#112233"}, {"id": 5, "name": "Deep work", "color": "#112233"}]
        self.link = connect(self.backend)
        self.pcm = speech_pcm(1.0)

    def say(self, vid=3, packets=None):
        packets = audio_messages(self.pcm, vid) if packets is None else packets
        self.backend.on_message({"t": "voice_start", "vid": vid, "rate": 16000})
        for p in packets:
            self.backend.on_message(p)
        return packets

    def end(self, vid, count):
        self.backend.on_message({"t": "voice_end", "vid": vid, "packets": count})

    def answer(self):
        return self.link.of_kind("transcript")

    def test_a_clip_comes_back_as_text_with_its_project(self):
        packets = self.say()
        self.end(3, len(packets))
        self.assertEqual(self.answer(), [{"t": "transcript", "vid": 3, "text": "Acme design review", "project": 123}])
        r = self.fake.requests[0]
        self.assertEqual(r["keywords"], ["Acme", "Deep work"])
        self.assertEqual(struct.unpack("<I", r["file"][24:28])[0], 16000)

    def test_no_project_named_means_no_project_field(self):
        self.fake.text = "Lunch"
        self.end(3, len(self.say()))
        self.assertEqual(self.answer(), [{"t": "transcript", "vid": 3, "text": "Lunch"}])

    def test_the_project_is_matched_in_the_whole_text_before_it_is_cut(self):
        self.fake.text = "x" * 200 + " Acme"
        self.end(3, len(self.say()))
        self.assertEqual(self.answer()[0]["project"], 123)
        self.assertLessEqual(len(self.answer()[0]["text"].encode()), 95)

    def test_packets_can_come_in_any_order_and_twice(self):
        packets = audio_messages(self.pcm)
        self.say(packets=list(reversed(packets)) + packets[:2])
        self.end(3, len(packets))
        self.assertEqual(self.answer()[0]["text"], "Acme design review")
        self.assertEqual(self.link.of_kind("transcript")[0]["vid"], 3)

    def test_a_missing_packet_means_failed_and_no_paid_request(self):
        packets = audio_messages(self.pcm)
        self.say(packets=packets[:3] + packets[4:])
        self.end(3, len(packets))
        self.assertEqual(self.answer(), [{"t": "transcript", "vid": 3, "error": "failed"}])
        self.assertEqual(self.fake.requests, [])

    def test_a_missing_last_packet_means_failed(self):
        packets = audio_messages(self.pcm)
        self.say(packets=packets[:-1])
        self.end(3, len(packets))
        self.assertEqual(self.answer()[0]["error"], "failed")

    def test_extra_packets_mean_failed(self):
        packets = audio_messages(self.pcm)
        self.say(packets=packets)
        self.end(3, len(packets) - 1)
        self.assertEqual(self.answer()[0]["error"], "failed")

    def test_an_absurd_packet_count_is_just_failed(self):
        self.say()
        self.end(3, 10 ** 12)
        self.assertEqual(self.answer()[0]["error"], "failed")

    def test_a_bad_packet_means_failed(self):
        for bad in ({"b64": "not base64!"}, {"p": 99999}, {"i": 89}, {"n": 999999}, {"n": -1}, {"seq": -1}, {"seq": "0"}, {"b64": 7}):
            with self.subTest(bad):
                self.link.sent.clear()
                packets = audio_messages(self.pcm)
                packets[0] = dict(packets[0], **bad)
                self.say(packets=packets)
                self.end(3, len(packets))
                self.assertEqual(self.answer()[-1]["error"], "failed")
        self.assertEqual(self.fake.requests, [])

    def test_silence_is_no_speech_and_no_paid_request(self):
        self.pcm = [0] * 16000
        self.end(3, len(self.say()))
        self.assertEqual(self.answer(), [{"t": "transcript", "vid": 3, "error": "no_speech"}])
        self.assertEqual(self.fake.requests, [])

    def test_an_empty_clip_is_no_speech(self):
        self.say(packets=[])
        self.end(3, 0)
        self.assertEqual(self.answer()[0]["error"], "no_speech")

    def test_the_service_hearing_nothing_is_no_speech(self):
        self.fake.text = " . "
        self.end(3, len(self.say()))
        self.assertEqual(self.answer()[0]["error"], "no_speech")

    def test_a_service_failure_is_failed(self):
        self.fake.script.extend([500])
        self.end(3, len(self.say()))
        self.assertEqual(self.answer()[0]["error"], "failed")

    def test_no_key_is_failed_with_no_request(self):
        backend = make_backend(speech=self.fake, store=MemoryStore())
        link = connect(backend)
        backend.on_message({"t": "voice_start", "vid": 3, "rate": 16000})
        packets = audio_messages(self.pcm)
        for p in packets:
            backend.on_message(p)
        backend.on_message({"t": "voice_end", "vid": 3, "packets": len(packets)})
        self.assertEqual(link.of_kind("transcript")[0]["error"], "failed")
        self.assertEqual(self.fake.requests, [])

    def test_a_cancelled_clip_gets_no_answer(self):
        packets = self.say()
        self.backend.on_message({"t": "voice_cancel", "vid": 3})
        self.assertIsNone(self.backend.voice.clip)
        self.assertEqual(self.answer(), [])
        self.assertEqual(self.fake.requests, [])
        self.say(vid=3, packets=packets)  # the same vid again is a new clip
        self.end(3, len(packets))
        self.assertEqual(self.answer()[0]["text"], "Acme design review")

    def test_a_cancel_after_the_end_drops_the_answer(self):
        jobs = []
        self.backend.voice.spawn = jobs.append
        self.end(3, len(self.say()))
        self.backend.on_message({"t": "voice_cancel", "vid": 3})
        jobs[0]()
        self.assertEqual(self.answer(), [])

    def test_a_new_clip_replaces_an_unfinished_one(self):
        packets = audio_messages(self.pcm, vid=4)
        self.say(vid=3)
        self.say(vid=4, packets=packets)
        self.end(4, len(packets))
        self.assertEqual([m["vid"] for m in self.answer()], [4])
        self.end(3, 12)  # its end still gets an answer, because the board waits for one
        self.assertEqual(self.answer()[-1], {"t": "transcript", "vid": 3, "error": "failed"})

    def test_an_end_for_a_clip_never_started_is_failed(self):
        self.end(9, 12)
        self.assertEqual(self.answer(), [{"t": "transcript", "vid": 9, "error": "failed"}])

    def test_a_repeated_end_is_answered_once(self):
        packets = self.say()
        self.end(3, len(packets))
        self.end(3, len(packets))
        self.assertEqual(len(self.answer()), 1)

    def test_audio_for_another_clip_is_ignored(self):
        self.say(vid=3)
        before = len(self.backend.voice.clip.packets)
        self.backend.on_message(audio_messages(self.pcm, vid=8)[0])
        self.assertEqual(len(self.backend.voice.clip.packets), before)

    def test_a_board_that_disconnects_mid_clip_loses_only_that_clip(self):
        packets = audio_messages(self.pcm)
        self.say(packets=packets[:2])
        disconnect(self.backend, self.link)
        self.assertIsNone(self.backend.voice.clip)
        self.assertEqual(self.answer(), [])
        self.assertEqual(self.fake.requests, [])
        link = connect(self.backend, self.link)
        for p in packets[2:]:   # the rest of the clip, if the board sent it, has no start any more
            self.backend.on_message(p)
        self.assertIsNone(self.backend.voice.clip)
        self.end(3, len(packets))
        self.assertEqual(link.of_kind("transcript"), [{"t": "transcript", "vid": 3, "error": "failed"}])
        self.say(vid=4, packets=audio_messages(self.pcm, vid=4))  # and the next clip works
        self.end(4, len(audio_messages(self.pcm, vid=4)))
        self.assertEqual(link.of_kind("transcript")[-1]["text"], "Acme design review")

    def test_an_answer_to_a_board_that_left_is_dropped(self):
        jobs = []
        self.backend.voice.spawn = jobs.append
        self.end(3, len(self.say()))
        disconnect(self.backend, self.link)
        connect(self.backend, self.link)
        jobs[0]()  # the service answers now, for the old session
        self.assertEqual(self.answer(), [])

    def test_a_clip_that_stops_arriving_fails(self):
        clock = Clock(0)
        self.backend.voice.clock = clock
        self.say()
        clock.advance(clips.IDLE_S - 1)
        self.backend.voice.tick()
        self.assertEqual(self.answer(), [])
        clock.advance(2)
        self.backend.voice.tick()
        self.assertEqual(self.answer(), [{"t": "transcript", "vid": 3, "error": "failed"}])
        self.assertIsNone(self.backend.voice.clip)

    def test_a_huge_clip_fails_instead_of_filling_memory(self):
        with unittest.mock.patch.object(clips, "MAX_CLIP_BYTES", 100):
            self.end(3, len(self.say()))
        self.assertEqual(self.answer()[0]["error"], "failed")
        self.assertEqual(self.fake.requests, [])

    def test_a_strange_rate_falls_back_and_a_clip_can_say_its_own(self):
        self.backend.on_message({"t": "voice_start", "vid": 5, "rate": 7})
        self.assertEqual(self.backend.voice.clip.rate, clips.DEFAULT_RATE)
        self.backend.on_message({"t": "voice_start", "vid": 6, "rate": 8000})
        self.assertEqual(self.backend.voice.clip.rate, 8000)

    def test_transcripts_stay_out_of_the_log(self):
        self.fake.text = f"{TRANSCRIPT_MARK} about Acme"
        start = helpers.LOGS.tell()
        self.end(3, len(self.say()))
        self.assertIn(TRANSCRIPT_MARK, self.answer()[0]["text"])
        helpers.LOGS.seek(start)
        self.assertNotIn(TRANSCRIPT_MARK, helpers.LOGS.read())
        helpers.LOGS.seek(0, 2)


class WithRunManagerProjects(HomeCase):
    def test_keywords_come_from_the_last_project_list(self):
        toggl, fake = FakeToggl(), FakeSpeech()
        self.addCleanup(toggl.close)
        self.addCleanup(fake.close)
        toggl.projects = [{"id": 1, "name": "Zed"}, {"id": 2, "name": "Acme"}]
        backend = make_backend(toggl, fake)
        link = connect(backend)
        backend.runs.run_jobs()
        count = toggl.count()
        pcm = speech_pcm(1.0)
        packets = audio_messages(pcm)
        backend.on_message({"t": "voice_start", "vid": 3, "rate": 16000})
        for p in packets:
            backend.on_message(p)
        backend.on_message({"t": "voice_end", "vid": 3, "packets": len(packets)})
        self.assertEqual(fake.requests[0]["keywords"], ["Acme", "Zed"])
        self.assertEqual(link.of_kind("transcript")[0]["project"], 2)
        self.assertEqual(toggl.count(), count)  # a voice clip costs no Toggl request


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
