from clipper.titles import format_clip_title


def test_explicit_aliases_and_redundant_labels_are_idempotent():
    for name, short in [("阿梓从小就很可爱", "阿梓"), ("雪糕cheese", "雪糕"),
                        ("七海Nana7mi", "七海Nana7mi"), ("阿萨Aza", "阿萨"),
                        ("雫るる_Official", "雫lulu")]:
        session = {"mode": "live", "streamer": name}
        result = format_clip_title(name + "：这段内容", session)
        assert result == f"【{short}】这段内容"
        assert format_clip_title(result, session) == result
        assert format_clip_title(f"【{name}】这段内容", session) == result


def test_topic_other_people_and_intro_are_preserved():
    session = {"mode": "live", "streamer": "阿梓从小就很可爱"}
    assert format_clip_title("【CS2】野牛还藏了一把", session) == "【阿梓】【CS2】野牛还藏了一把"
    assert format_clip_title("雪糕：这段内容", session) == "【阿梓】雪糕：这段内容"
    assert format_clip_title("小担接管账号", {"mode": "file", "streamer": "阿梓"}) == "小担接管账号"
    assert format_clip_title("原始标题", {"mode": "live"}) == "原始标题"
    assert len(format_clip_title("内容" * 100, session)) == 80


def test_repeated_source_subject_is_removed_before_a_predicate():
    session = {"mode": "live", "streamer": "阿萨Aza"}
    assert format_clip_title("【阿萨Aza】阿萨挑战人机验证", session) == "【阿萨】挑战人机验证"
    assert format_clip_title("阿萨Aza被验证码难住了", session) == "【阿萨】被验证码难住了"
    result = format_clip_title("【阿萨】阿萨吐槽这也要验证", session)
    assert result == "【阿萨】吐槽这也要验证"
    assert format_clip_title(result, session) == result


def test_source_label_does_not_erase_joint_subjects_names_or_other_speakers():
    session = {"mode": "live", "streamer": "阿梓从小就很可爱"}
    for title in ["阿梓和雪糕一起挑战", "阿梓、七海都选了同一个答案", "阿梓妈妈的评价", "雪糕吐槽阿梓"]:
        assert format_clip_title(title, session) == "【阿梓】" + title
