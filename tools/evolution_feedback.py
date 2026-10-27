"""Read-only per-work feedback query for future AutoQuill evolution reviews."""
import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import evolution, paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, help='AutoQuill用户数据根目录；不会修改它')
    parser.add_argument('--horizon', type=int, choices=(7, 14, 30, 90), default=30)
    parser.add_argument('--metric', choices=('reads', 'likes', 'comments', 'collects', 'favors'), default='likes')
    parser.add_argument('--since', default='', help='筛选作品发布日期，自YYYY-MM-DD')
    parser.add_argument('--until', default='', help='筛选作品发布日期，至YYYY-MM-DD')
    parser.add_argument('--aid', default='', help='只查询这个作品ID')
    parser.add_argument('--node-id', default='', help='只查询已确认属于这个演进行动的作品')
    parser.add_argument('--as-of', default='', help='固定查询截止日期YYYY-MM-DD；排除之后的观测')
    parser.add_argument('--version', default='', help='只查询已确认属于这个生成版本的作品，如5.3.0')
    parser.add_argument('--attribution-basis', choices=('publication', 'evidence'), default='evidence',
                        help='publication按发布时间区间推定；evidence按生成证据（兼容旧查询）')
    parser.add_argument('--output', type=Path, help='另存JSON报告；省略时输出到终端')
    args = parser.parse_args(argv)
    previous = paths.DATA_ROOT
    try:
        if args.data_root is not None:
            paths.DATA_ROOT = args.data_root.resolve()
        options = dict(as_of=args.as_of or None, version=args.version)
        if args.attribution_basis != 'evidence':
            options['attribution_basis'] = args.attribution_basis
        report = evolution.contract(args.horizon, args.metric, args.since,
                                    args.until, args.aid, args.node_id, **options)
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output is None:
            print(text)
        else:
            target = args.output.resolve()
            root = Path(paths.DATA_ROOT).resolve()
            # 查询不能将报告覆盖到输入快照或运行状态中。
            for protected in (root / 'data', root / 'config'):
                if target == protected or protected in target.parents:
                    parser.error('报告请另存到data/config之外，避免覆盖业务原件')
            if target.exists():
                parser.error('输出文件已存在，请使用新文件名保留已有成果')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text + '\n', encoding='utf-8')
            print(str(target))
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        paths.DATA_ROOT = previous
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
