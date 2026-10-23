import pytest

from clipper.live_publishing import check_numeric_claims


@pytest.mark.parametrize("copy,evidence", [
    ("体重208kg", "我现在二百零八公斤"),
    ("体重208kg", "两百零八公斤"),
    ("体重二百零八公斤", "208公斤"),
    ("猜密码254", "密码二五四"),
    ("猜密码二五四", "密码254"),
    ("密码0208", "密码零二零八"),
    ("身高1.65米", "身高一点六五米"),
    ("花了208.05元", "花了二百零八点零五元"),
    ("花了二百零八点零五元", "花了208.05元"),
    ("刚好208.", "二百零八"),
    ("刚好208", "208."),
    ("10008次", "一万零八次"),
    ("10200次", "一万零二百次"),
    ("12000次", "一万二千次"),
    ("万一猜对了", "猜中了"),
    ("三心二意", "不要犹豫"),
])
def test_exact_numeric_readings(copy, evidence):
    check_numeric_claims(copy, evidence)


@pytest.mark.parametrize("copy,evidence", [
    ("208kg", "1208公斤"),
    ("208kg", "2080公斤"),
    ("208kg", "一千二百零八公斤"),
    ("208kg", "二千零八十公斤"),
    ("208kg", "1.208公斤"),
    ("208kg", "208.5公斤"),
    ("208.5kg", "二百零八公斤"),
    ("208kg", "二百零九公斤"),
    ("208kg", "二零八零公斤"),
    ("208kg", "零二零八公斤"),
    ("0208", "二百零八"),
    ("120次", "一百二次"),
    ("102次", "一百二次"),
    ("30次", "一二三十次"),
    ("10001次", "万一失败"),
    ("100000000次", "一亿次"),
    ("254", "二五四九"),
    ("25", "二五四"),
    ("246成功开启", "所以密码是二五四"),
    ("二四六成功开启", "所以密码是二五四"),
    ("1.5米", "一点零五米"),
    ("1.05米", "一点五米"),
    ("1.5米", "1.5.0米"),
    ("1.5.0米", "1.5米"),
    ("208", ".208"),
    ("208", "1..208"),
    ("208kg", "2\n08公斤"),
    ("一百零八次", "一百零九次"),
])
def test_other_numbers_do_not_supply_evidence(copy, evidence):
    with pytest.raises(ValueError, match="editorial_numeric_claim_without_evidence"):
        check_numeric_claims(copy, evidence)


def test_real_208_kg_citation_keeps_the_kilo_unit_out_of_the_number():
    cited_text = ("你知道什么概念吗？二百零八千克是四百\n"
                  "四百一十六斤。我坐在这个东西上，\n那是半吨啊，大家，")
    check_numeric_claims("被说208kg后：我直接卡在椅子上起不来了", cited_text)


@pytest.mark.parametrize("unit", ["千克", "千米", "千瓦", "千瓦时", "千伏", "千安",
                                  "千欧", "千赫兹", "千焦", "千帕", "千牛", "千升", "千卡"])
def test_explicit_kilo_measurements_have_a_numeric_boundary(unit):
    check_numeric_claims("208" + unit, "二百零八" + unit)
    check_numeric_claims("二百零八" + unit, "208" + unit)
    with pytest.raises(ValueError, match="editorial_numeric_claim_without_evidence"):
        check_numeric_claims("208" + unit, "二千零八" + unit)


@pytest.mark.parametrize("copy,evidence", [
    ("2.08千克", "二点零八千克"),
    ("1000千克", "一千千克"),
    ("1000元", "一千元"),
    ("2000人", "二千人"),
])
def test_unit_boundaries_do_not_remove_numeric_thousands(copy, evidence):
    check_numeric_claims(copy, evidence)


@pytest.mark.parametrize("copy,evidence", [
    ("208kg", "二千零八千克"),
    ("208kg", "二点零八千克"),
    ("208kg", "零二零八千克"),
    ("1元", "一千元"),
    ("2人", "二千人"),
    ("208元", "二百零八千元"),
])
def test_kilo_boundary_cannot_supply_a_different_number(copy, evidence):
    with pytest.raises(ValueError, match="editorial_numeric_claim_without_evidence"):
        check_numeric_claims(copy, evidence)
