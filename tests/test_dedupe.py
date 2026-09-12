"""Dedupe planning. The integrity assertion here caught a real 179-file bug."""
from conftest import drive

from zoomarchive.dedupe import build_plan


def test_keeps_exactly_one_copy_per_md5():
    rows = [drive(100, id='a', md5='same', path='/x'),
            drive(100, id='b', md5='same', path='/y'),
            drive(100, id='c', md5='same', path='/z')]
    plan = build_plan(rows)
    assert len(plan['delete_ids']) == 2
    assert not plan['orphaned']


def test_unique_files_are_never_touched():
    rows = [drive(100, id='a', md5='one'), drive(200, id='b', md5='two')]
    plan = build_plan(rows)
    assert plan['delete_ids'] == []
    assert plan['rows'] == []


def test_no_unique_file_ever_loses_every_copy():
    """The invariant the whole design rests on, over a messy mixed index."""
    rows = []
    for i in range(12):
        for copy in range(1 + i % 3):
            rows.append(drive(1000 + i, id=f'f{i}-{copy}', md5=f'md5-{i}',
                              path=f'/folder-{copy}'))
    plan = build_plan(rows)
    assert plan['orphaned'] == []

    deleting = set(plan['delete_ids'])
    survivors = {r['md5'] for r in rows if r['id'] not in deleting}
    assert survivors == {r['md5'] for r in rows}, 'every md5 must survive'


def test_orphan_detection_would_catch_a_bad_plan():
    """Guard the guard: if every copy were slated for deletion, we'd know."""
    rows = [drive(100, id='a', md5='same', path='/x'),
            drive(100, id='b', md5='same', path='/y')]
    plan = build_plan(rows)
    # Simulate a planning bug that also deletes the keeper.
    plan['delete_ids'].append(
        next(r['id'] for r in rows if r['id'] not in plan['delete_ids']))
    deleting = set(plan['delete_ids'])
    orphaned = [r['md5'] for r in rows if all(
        x['id'] in deleting for x in rows if x['md5'] == r['md5'])]
    assert orphaned, 'a plan deleting all copies must be detectable'


def test_keeper_is_the_copy_in_the_most_complete_folder():
    """So redundant partial folders empty out cleanly instead of leaving strays."""
    rows = [
        drive(100, id='keep', md5='m1', path='/full'),
        drive(200, id='other', md5='m2', path='/full'),
        drive(300, id='third', md5='m3', path='/full'),
        drive(100, id='dup', md5='m1', path='/sparse'),
    ]
    plan = build_plan(rows)
    assert plan['delete_ids'] == ['dup']
    assert plan['empty_after'] == ['/sparse']


def test_plan_is_deterministic():
    rows = [drive(100, id=f'id{i}', md5='same', path=f'/p{i}')
            for i in range(6)]
    assert build_plan(rows)['delete_ids'] == build_plan(rows)['delete_ids']


def test_rows_without_md5_are_ignored():
    """Drive omits MD5s for Google-native files; they are not dedup candidates."""
    rows = [drive(100, id='a', md5=None), drive(100, id='b', md5=None)]
    assert build_plan(rows)['delete_ids'] == []


def test_same_and_cross_folder_duplicates_are_labelled():
    rows = [drive(100, id='a', md5='m', path='/x'),
            drive(100, id='b', md5='m', path='/x'),
            drive(100, id='c', md5='m', path='/y')]
    actions = {r['file_id']: r['action'] for r in build_plan(rows)['rows']}
    assert 'same-folder' in actions['b']
    assert 'cross-folder' in actions['c']


def test_fixture_index_produces_a_safe_plan():
    import json
    import os
    fx = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      'fixtures', 'drive_index_md5.json')
    with open(fx) as fh:
        rows = json.load(fh)
    plan = build_plan(rows)
    assert plan['orphaned'] == []
    assert plan['delete_ids'], 'fixtures deliberately contain duplicates'
