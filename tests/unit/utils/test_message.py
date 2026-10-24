# SPDX-FileCopyrightText: qutebrowser contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for async message prompts."""

from qutebrowser.utils import message, usertypes


def test_ask_async_cancelled(monkeypatch, qapp):
    asked = []
    monkeypatch.setattr(
        message.global_bridge, 'ask',
        lambda question, blocking: asked.append(question))

    calls = []
    message.ask_async(
        "Unlock", usertypes.PromptMode.password,
        lambda answer: calls.append(('answer', answer)),
        cancelled=lambda: calls.append(('cancelled', None)),
    )
    assert len(asked) == 1
    asked[0].cancel()
    assert calls == [('cancelled', None)]


def test_ask_async_answered(monkeypatch, qapp):
    asked = []
    monkeypatch.setattr(
        message.global_bridge, 'ask',
        lambda question, blocking: asked.append(question))

    calls = []
    message.ask_async(
        "Unlock", usertypes.PromptMode.password,
        lambda answer: calls.append(('answer', answer)),
        cancelled=lambda: calls.append(('cancelled', None)),
    )
    asked[0].answer = 'secret'
    asked[0].done()
    assert calls == [('answer', 'secret')]
