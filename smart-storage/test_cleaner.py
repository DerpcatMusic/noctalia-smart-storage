"""Smallest check that fails if the classification, walk or chart logic breaks: python3 test_cleaner.py"""
import time, cleaner
now = time.time()
items = [{'eligible':True,'latest':now-3*cleaner.DAY},{'eligible':True,'latest':now-30*cleaner.DAY}]
assert [x['eligible'] for x in cleaner.aged(items,{'age_days':0})] == [True,True]
assert [x['eligible'] for x in cleaner.aged(items,{'age_days':7})] == [False,True]
assert cleaner.shade('#ffffff',-0.5) == '#7f7f7f' and cleaner.shade('#000000',0.5) == '#7f7f7f'
assert cleaner.LOG_NAME.search('launcher.log') and cleaner.LOG_NAME.search('x.log.1') and not cleaner.LOG_NAME.search('catalog.json')
assert set(cleaner.DEFAULT['categories']).isdisjoint({'trash','worktrees','branches'})
assert cleaner.BACKUP.search('buffr-drafts-backup-20260824') and cleaner.BACKUP.search('settings.toml.bak-x') and not cleaner.BACKUP.search('bakery')
m = cleaner.VERSION.match('bin.2.336.0'); assert m.group(1)=='bin' and m.group(2)=='2.336.0'
m = cleaner.VERSION.match('ElementalWarrior-wine-11.12-v4'); assert m.group(2)=='11.12' and m.group(3)=='-v4'

if __name__ == '__main__':  # pool workers re-import this file; only the parent builds trees
    import concurrent.futures, multiprocessing, os, tempfile
    from pathlib import Path
    live = {'errors':[],'paths':[],'names':[],'refs':set(),'mounts':set()}
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        # Pool walk splits into pieces (> BUDGET entries) and must match the inline walk stage() re-checks with.
        for i in range(6000):
            (d/'tree'/str(i%60)).mkdir(parents=True, exist_ok=True)
            (d/'tree'/str(i%60)/f'f{i}').write_bytes(b'x'*(i%9))
        (d/'tree/7/f7').write_bytes(b'x'*65536)
        os.link(d/'tree/0/f0', d/'tree/1/hard')  # every link inside: reclaimable
        os.link(d/'tree/7/f7', d/'outside')      # a link outside: counted, not reclaimable
        inline = cleaner.inventory(d/'tree', live, cleaner.DEFAULT, 'temp')
        with concurrent.futures.ProcessPoolExecutor(4, mp_context=multiprocessing.get_context('forkserver'),
                                                    initializer=cleaner._init, initargs=(set(),set())) as pool:
            pooled = cleaner.inventory(d/'tree', live, cleaner.DEFAULT, 'temp', pool)
        assert all(inline[k]==pooled[k] for k in ('fingerprint','bytes','reclaimable','files')), (inline, pooled)
        assert inline['files'] == 6062 and inline['reclaimable'] < inline['bytes'] and inline['eligible']

        # Staging reuses a folder's fingerprint after the move; deleting is a rename now and a reap later.
        cleaner.STATE, cleaner.REAP = d/'state', d/'state/reap.json'
        cleaner.STATE.mkdir()
        cleaner.reap_later = lambda: None  # a spawned reaper would use the real state dir
        (d/'tree').rename(d/'moved')
        assert cleaner.inventory(d/'moved', live, cleaner.DEFAULT, 'temp')['fingerprint'] == inline['fingerprint']
        (d/'moved/0').chmod(0o555)  # read-only, like Go's module cache
        cleaner.move_aside(d/'moved')
        assert not (d/'moved').exists() and len(cleaner.read(cleaner.REAP, [])) == 1
        cleaner.reap()
        assert cleaner.read(cleaner.REAP, []) == [] and not any(x.name.startswith('.smart-storage') for x in d.iterdir())

        w = d/'walk'
        for p in ('proj/.git','proj/target/debug/.fingerprint','app-1.2','app-1.10','foo-1.0','foo_1.0','old-backup/inner/target/x/.fingerprint','pkg'):
            (w/p).mkdir(parents=True)
        for t in (w/'proj/target', w/'old-backup/inner/target'):
            (t/'.rustc_info.json').write_text('{}')
            (t/'CACHEDIR.TAG').write_text('Signature: 8a477f597d28d172789f06886806bc55')
        (w/'pkg.zip').write_bytes(b'z')
        found, notes = [], {}
        for part in cleaner.fan_out(None, cleaner.discover, [str(w)]):  # equal versions (foo-1.0, foo_1.0) must not crash
            found += part['found']
            notes |= part['notes']
        s = str(w)
        assert {(s+'/proj','repo'), (s+'/proj/target','builds'), (s+'/old-backup/inner/target','builds')} <= set(found), found
        assert notes[s+'/app-1.2'] == 'older version · current is app-1.10' and s+'/app-1.10' not in notes
        assert notes[s+'/old-backup'] == 'backup by name' and notes[s+'/pkg.zip'].startswith('archive already extracted')

        # Duplicates never flag the last unflagged copy, even when the oldest copy sits in a flagged folder.
        for i, sub in enumerate(('bk-backup','b','c')):
            (d/sub).mkdir()
            (d/sub/'x.iso').write_bytes(b'same'*300)
            os.utime(d/sub/'x.iso', (i+1, i+1))
        notes = {str(d/'bk-backup'):'backup by name'}
        big = [(1200, str(d/sub/'x.iso'), 0, i, i+1.0) for i, sub in enumerate(('bk-backup','b','c'))]
        with concurrent.futures.ThreadPoolExecutor() as threads: cleaner.duplicates(notes, big, threads)
        assert str(d/'c/x.iso') in notes and str(d/'b/x.iso') not in notes and cleaner.nested(str(d/'bk-backup/x.iso'), notes)
    print('ok')
