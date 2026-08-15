"""청크 언어 판별 (`06 §2` ③ — 검색 축을 언어별로 나누기 위한 것).

임베딩은 **같은 언어끼리** 비교할 때 가장 정확하다. 질문을 영어 하나로만 번역해서 검색하면
한국어 문서는 교차언어 매칭이 되어 뜻이 같아도 코사인이 크게 떨어진다.

실측(2026-08-16, `probe_seed_docs.py`): 한국어 청크를 영어 질의로 찾을 때
`백업은 어떤 볼륨들을 함께 받아야 하나요?` → **0.1914**. 청크 본문이 질문과 거의 같은
문장인데도 `similarity_floor`(0.444)에 걸려 강제 🔴 `no_evidence` 였다.

⛔ 그 해결책이 "한국어 문서에 영어 요약을 달아 두세요" 가 되면 안 된다. 각자의 언어로
   쓴 문서를 그대로 두고도 협업이 되게 하는 것이 이 제품의 목적이다.

### 문자 체계로만 판별한다 — LLM 을 부르지 않는다
문서 하나에 호출을 더하면 인제스트가 느려지고 비용이 붙는데, 우리가 구분해야 하는 축은
**임베딩 공간이 갈리는 지점**이라 문자 체계면 충분하다. 판별은 결정적이고 재현 가능하다.

⚠️ **한계**: 라틴 문자를 쓰는 언어(영어·프랑스어·독일어…)는 전부 `en` 으로 묶인다. 그
   언어들끼리는 임베딩 공간이 비교적 가까워 검색이 성립하므로 지금은 문제가 아니지만,
   비라틴 언어를 더 다루게 되면 여기부터 손봐야 한다.
"""

import re

# 한국어·일본어·중국어는 문자 영역이 갈린다. 라틴 문자는 전부 기본값으로 떨어진다.
_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ]")
_KANA = re.compile(r"[぀-ヿ]")
_HAN = re.compile(r"[一-鿿]")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)

DEFAULT_LANGUAGE = "en"

# 이 비율을 넘는 글자가 한 문자 체계에 속하면 그 언어로 본다.
#
# ⚠️ 낮게 잡는다. 기술 문서는 한국어 문장에도 식별자·코드가 잔뜩 섞여 라틴 문자 비중이
#    쉽게 절반을 넘는다 — 0.5 로 잡으면 한국어 문서가 `en` 으로 오판된다.
_SCRIPT_RATIO = 0.15


def detect(text: str) -> str:
    """`ko` · `ja` · `zh` · `en` 중 하나. 글자가 없으면 기본값이다.

    >>> detect("액세스 토큰의 유효기간은 `access_token_expire_minutes` 로 정한다.")
    'ko'
    >>> detect("The access token is valid for 30 minutes.")
    'en'
    """
    letters = _LETTER.findall(text)
    if not letters:
        return DEFAULT_LANGUAGE
    total = len(letters)

    # 순서가 중요하다. 일본어 문장에는 한자가 섞이고 한국어 문장에도 한자가 섞일 수 있으므로
    # **가나 → 한글 → 한자** 순으로 본다. 한자는 마지막에 남은 경우에만 중국어로 본다.
    if len(_KANA.findall(text)) / total >= _SCRIPT_RATIO:
        return "ja"
    if len(_HANGUL.findall(text)) / total >= _SCRIPT_RATIO:
        return "ko"
    if len(_HAN.findall(text)) / total >= _SCRIPT_RATIO:
        return "zh"
    return DEFAULT_LANGUAGE
