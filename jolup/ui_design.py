"""Woo Sujin's visual design, fed only by the existing verified domain results."""
from __future__ import annotations

import base64
from functools import lru_cache
from html import escape
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "sujin"
AVATAR = ASSETS / "rabbit-avatar.png"


@lru_cache(maxsize=2)
def image_uri(name: str) -> str:
    return "data:image/png;base64," + base64.b64encode((ASSETS / name).read_bytes()).decode("ascii")


def theme_css() -> str:
    return "<style>" + (ASSETS / "theme.css").read_text(encoding="utf-8") + "</style>"


def hero_html() -> str:
    avatar = image_uri("rabbit-avatar.png")
    hero = image_uri("rabbit-hero.png")
    return (f'<div class="j-brand"><img src="{avatar}" alt="졸업각 토끼">졸업각'
            '<small>UniPath AI · 우수진 디자인</small></div>'
            '<section class="j-hero"><div><span class="j-tag">✦ 성결대학교 졸업 지원 에이전트</span>'
            '<h1>졸업까지 남은 길,<br><mark>토끼</mark>가 같이 걸어줄게요</h1>'
            '<p>“나 졸업 괜찮아?”<br>이수한 학점과 학교 근거부터 다음 학기 수강 계획, 학사 일정까지 확인해요.</p>'
            '<p>왼쪽에서 더미 학생을 고르거나 CSV 성적표로 시작하세요.</p></div>'
            f'<div class="j-mascot"><div class="j-bubble">지금 이수와 앞으로의 계획을 나눠서!</div>'
            f'<br><img src="{hero}" alt="학사모를 쓴 졸업각 토끼"></div></section>')


def steps_html(has_courses: bool) -> str:
    labels = ("① 성적표 · 더미 선택", "② 현재 요건 확인", "③ 수강 계획", "④ 일정 확인 · 승인")
    return '<div class="j-steps">' + "".join(
        f'<span class="{"done" if index == 0 and has_courses else ""}">{label}</span>'
        for index, label in enumerate(labels)) + "</div>"


def requirement_cards(rows: list[dict]) -> str:
    cards = []
    for row in rows:
        if row.get("unit") != "학점" or not row.get("required"):
            continue
        earned, required = row.get("earned"), row["required"]
        progress = min(max(float(earned or 0) / float(required) * 100, 0), 100)
        count = "확인 필요" if earned is None else f"{earned:g} / {required:g}학점"
        cards.append(f'<article class="j-requirement"><b>{escape(str(row["area"]))}</b>'
                     f'<div class="j-track"><i style="width:{progress:.1f}%"></i></div>'
                     f'<span>{count} · {escape(str(row.get("status", "확인 필요")))}</span></article>')
    return '<div class="j-requirements">' + "".join(cards) + "</div>"


def panel_title(title: str) -> str:
    return f'<div class="j-panel-title">{escape(title)}</div>'
