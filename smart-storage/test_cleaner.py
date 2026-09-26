"""Smallest check that fails if the classification, walk or chart logic breaks: python3 test_cleaner.py"""
import time, cleaner
now = time.time()
items = [{'eligible':True,'latest':now-3*cleaner.DAY},{'eligible':True,'latest':now-30*cleaner.DAY}]
assert [x['eligible'] for x in cleaner.aged(items,{'age_days':0})] == [True,True]
assert [x['eligible'] for x in cleaner.aged(items,{'age_days':7})] == [False,True]
import probe, pathlib
assert {'/', '/proc'} <= probe.mount_paths(pathlib.Path('/proc/self'))  # host mounts map onto themselves
assert cleaner.shade('#ffffff',-0.5) == '#7f7f7f' and cleaner.shade('#000000',0.5) == '#7f7f7f'
assert cleaner.LOG_NAME.search('launcher.log') and cleaner.LOG_NAME.search('x.log.1') and not cleaner.LOG_NAME.search('catalog.json')
assert set(cleaner.DEFAULT['categories']).isdisjoint({'trash','worktrees','branches'})

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
        for p in ('proj/.git','proj/target/debug/.fingerprint','old-backup/inner/target/x/.fingerprint',
                  'Song/auto-backups','Song Project/Backup','Song Project/Ableton Project Info','Vox/Backup','Stems/auto-backups'):
            (w/p).mkdir(parents=True)
        for t in (w/'proj/target', w/'old-backup/inner/target'):
            (t/'.rustc_info.json').write_text('{}')
            (t/'CACHEDIR.TAG').write_text('Signature: 8a477f597d28d172789f06886806bc55')
        (w/'Song/Song.bwproject').write_bytes(b'b')
        for f in ('Vox/Verse 1 Backup R_1.wav', 'Vox/take.wav.bak', 'Stems/kick.wav', 'Stems/snare.wav'):
            (w/f).write_bytes(b'\0'*4096)  # same size, same bytes: still never "duplicates"
        found, notes = [], {}
        for part in cleaner.fan_out(None, cleaner.discover, [str(w)]):
            found += part['found']
            notes |= part['notes']
        s = str(w)
        assert {(s+'/proj','repo'), (s+'/proj/target','builds'), (s+'/old-backup/inner/target','builds')} <= set(found), found
        # Only folders a DAW wrote next to its project file: no name guesses, no files, no duplicates.
        assert notes == {s+'/Song/auto-backups':'Bitwig auto-backups', s+'/Song Project/Backup':'Ableton project backups'}, notes
        assert cleaner.walked(s+'/Song/auto-backups','redundant',[s]) and not cleaner.walked(s+'/Vox/Backup','redundant',[s])
    print('ok')
import tempfile
with tempfile.TemporaryDirectory() as t:  # cargo 1.98 targets: tag, no .rustc_info.json; a foreign tag is not cargo's
    tag = pathlib.Path(t,'CACHEDIR.TAG')
    tag.write_text('Signature: 8a477f597d28d172789f06886806bc55\n# This file is a cache directory tag created by cargo.\n')
    assert cleaner.cargo_target(t)
    tag.write_text('Signature: 8a477f597d28d172789f06886806bc55\n# created by ccache\n')
    assert not cleaner.cargo_target(t)
