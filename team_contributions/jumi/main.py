from rag.answer_generator import answer_question, format_source


def main():
    print("================================")
    print("🎓 졸업 및 학사 규정 검색 RAG 도우미")
    print("종료하려면 q, quit, exit를 입력하세요.")
    print("================================")

    while True:
        try:
            question = input("\n질문: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n종료합니다.")
            break

        if not question:
            continue

        if question.lower() in {"q", "quit", "exit"}:
            print("종료합니다.")
            break

        try:
            print("\n[답변 생성 중...]")
            result = answer_question(question)

            print("\n[답변]")
            print(result["answer"])

            print("\n[출처]")
            if result["sources"]:
                for item in result["sources"]:
                    print(f"- {format_source(item)}")
            else:
                print("확인된 출처가 없습니다.")

        except Exception as error:
            print(f"\n[오류 발생] {error}")


if __name__ == "__main__":
    main()