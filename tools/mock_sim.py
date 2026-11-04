# -*- coding: utf-8 -*-
"""一键模拟互动：对本地主服务注入剧本事件，全链路验收不用真开播。

用法：
    python tools/mock_sim.py                    # 跑完整剧本一轮
    python tools/mock_sim.py --scenario gift    # 只测礼物连击
    python tools/mock_sim.py --loop 3 --interval 1.2
    python tools/mock_sim.py --url http://127.0.0.1:8188
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

SCENARIOS = {
    "full": [
        {"msg_type": 6, "nickname": "阿拉贡", "sec_open_id": "u_alagon"},
        {"msg_type": 2, "nickname": "阿拉贡", "sec_open_id": "u_alagon", "content": "小鱼今天状态不错啊"},
        {"msg_type": 1, "nickname": "路人甲", "sec_open_id": "u_a1", "like_num": 3},
        {"msg_type": 1, "nickname": "路人甲", "sec_open_id": "u_a1", "like_num": 2},
        {"msg_type": 5, "nickname": "路人甲", "sec_open_id": "u_a1", "user_follow_action": 1},
        {"msg_type": 3, "nickname": "阿拉贡", "sec_open_id": "u_alagon", "gift_name_plain": "玫瑰", "gift_num": 1},
        {"msg_type": 3, "nickname": "阿拉贡", "sec_open_id": "u_alagon", "gift_name_plain": "玫瑰", "gift_num": 2},
        {"msg_type": 3, "nickname": "阿拉贡", "sec_open_id": "u_alagon", "gift_name_plain": "跑车", "gift_num": 1},
        {"msg_type": 2, "nickname": "阿拉贡", "sec_open_id": "u_alagon", "content": "记住我最喜欢的歌是夜曲"},
    ],
    "chat": [
        {"msg_type": 2, "nickname": "路人甲", "sec_open_id": "u_a1", "content": "在吗在吗"},
        {"msg_type": 2, "nickname": "路人乙", "sec_open_id": "u_b1", "content": "讲个笑话"},
        {"msg_type": 2, "nickname": "阿拉贡", "sec_open_id": "u_alagon", "content": "昨晚看你播到三点？"},
    ],
    "gift": [
        {"msg_type": 3, "nickname": "大哥", "sec_open_id": "u_big", "gift_name_plain": "跑车", "gift_num": 1},
        {"msg_type": 3, "nickname": "大哥", "sec_open_id": "u_big", "gift_name_plain": "跑车", "gift_num": 1},
        {"msg_type": 3, "nickname": "大哥", "sec_open_id": "u_big", "gift_name_plain": "嘉年华", "gift_num": 1},
    ],
    "combo": [
        {"msg_type": 1, "nickname": "路人甲", "sec_open_id": "u_a1", "like_num": 1},
        {"msg_type": 1, "nickname": "路人甲", "sec_open_id": "u_a1", "like_num": 1},
        {"msg_type": 1, "nickname": "路人甲", "sec_open_id": "u_a1", "like_num": 5},
    ],
}


def inject(url: str, payload: dict) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url + "/api/inject", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as exc:
        print(f"[mock_sim] inject failed: {exc}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8188")
    parser.add_argument("--scenario", default="full", choices=list(SCENARIOS))
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--loop", type=int, default=1)
    args = parser.parse_args()

    for round_no in range(args.loop):
        print(f"== round {round_no + 1}/{args.loop} · {args.scenario} ==")
        for i, item in enumerate(SCENARIOS[args.scenario]):
            payload = dict(item)
            payload["msg_id"] = f"sim-{round_no}-{i}-{time.time_ns()}"
            ok = inject(args.url, payload)
            print(f"  [{ 'OK' if ok else 'FAIL' }] {payload.get('nickname', '')} {payload.get('content') or payload.get('gift_name_plain') or payload.get('msg_type')}")
            time.sleep(args.interval)
    print("done.")


if __name__ == "__main__":
    main()
