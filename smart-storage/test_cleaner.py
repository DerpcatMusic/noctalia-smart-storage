"""Smallest check that fails if the classification or chart logic breaks: python3 test_cleaner.py"""
import time, cleaner
now = time.time()
items = [{'eligible':True,'latest':now-3*cleaner.DAY},{'eligible':True,'latest':now-30*cleaner.DAY}]
assert [x['eligible'] for x in cleaner.aged(items,{'age_days':0})] == [True,True]
assert [x['eligible'] for x in cleaner.aged(items,{'age_days':7})] == [False,True]
assert cleaner.shade('#ffffff',-0.5) == '#7f7f7f' and cleaner.shade('#000000',0.5) == '#7f7f7f'
assert cleaner.LOG_NAME.search('launcher.log') and cleaner.LOG_NAME.search('x.log.1') and not cleaner.LOG_NAME.search('catalog.json')
assert cleaner.png(1,[b'\0\0\0\0'])[:8] == b'\x89PNG\r\n\x1a\n'
assert set(cleaner.DEFAULT['categories']).isdisjoint({'trash','worktrees','branches'})
print('ok')

assert cleaner.BACKUP.search('buffr-drafts-backup-20260824') and cleaner.BACKUP.search('settings.toml.bak-x') and not cleaner.BACKUP.search('bakery')
m = cleaner.VERSION.match('bin.2.336.0'); assert m.group(1)=='bin' and m.group(2)=='2.336.0'
m = cleaner.VERSION.match('ElementalWarrior-wine-11.12-v4'); assert m.group(2)=='11.12' and m.group(3)=='-v4'
