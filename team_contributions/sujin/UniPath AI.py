from pathlib import Path
import streamlit as st
import streamlit.components.v1 as components

# 1. 화면을 넓게 쓰도록 설정
st.set_page_config(layout="wide", page_title="졸업각 프로젝트")

# 2. (선택) MD 파일 내용 상단에 띄우기
# 만약 요약문이나 설명이 적힌 md 파일이 있다면 자동으로 읽어서 띄워줍니다.
try:
    with open("졸업각site.md", "r", encoding="utf-8") as f:

        md_content = f.read()
    st.markdown(md_content)
    st.write("---") # 구분선
except FileNotFoundError:
    pass # md 파일 이름이 다르거나 없으면 그냥 넘어갑니다.

# 3. HTML 파일 통째로 읽어서 하단에 띄우기
with open(Path(__file__).with_name("졸업각site - 복사본 (2).html"), "r", encoding="utf-8") as f:
    html_content = f.read()

# 높이(height)는 화면 크기에 맞게 800~1000 등으로 조절하시면 됩니다.
components.html(html_content, height=900, scrolling=True)
