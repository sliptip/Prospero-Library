import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Clipboard, Download, FileText, Folder, History, Plus, RefreshCw, RotateCcw, ScanLine, Trash2, X } from 'lucide-react'
import { CompatiblePatch, Dialog } from './Game'
import { Cover } from './Library'
import { api, dateText, json, patchFirmwareText, patchLabel, unknown } from './model'
import type { Game, Job, Root, Settings } from './model'

export function Updates({ games, open, start, busy }: {
  games: Game[]; open: (id: number) => void; start: (id?: number) => void; busy: boolean;
}) {
  const [expanded, setExpanded] = useState<number | null>(null)
  return <>
    <div className="page-intro section-heading">
      <div><h1>Обновления</h1><p>Официальные патчи и требования к прошивке</p></div>
      <button className="button primary" disabled={busy || !games.length} onClick={() => start()}><RefreshCw size={17} /> Проверить все</button>
    </div>
    <div className="management-stack">
      {!games.length && <div className="empty-state"><p>Добавьте игры в библиотеку.</p></div>}
      {games.map(game => <section key={game.id} className="panel update-card">
        <div className="update-title">
          <Cover game={game} />
          <div><button className="text-button" onClick={() => open(game.id)}>{game.title}</button><div className="muted mono">{game.title_id}</div></div>
          <span className={'patch-status ' + game.patch.state}>{patchLabel[game.patch.state]}</span>
          <button className="icon-button" onClick={() => start(game.id)} disabled={busy} title="Проверить игру" aria-label={'Проверить ' + game.title}><RefreshCw size={17} /></button>
        </div>
        <dl className="meta-grid update-meta">
          <div><dt>В коллекции</dt><dd className="mono">{game.collection_version || unknown}</dd></div>
          <div><dt>Последний официальный</dt><dd className="mono">{game.patch.latest?.version || unknown}</dd></div>
          <div><dt>Прошивка патча</dt><dd className="mono" title={game.patch.latest?.firmware ?? undefined}>
            {patchFirmwareText(game.patch.latest?.firmware)}</dd></div>
          <CompatiblePatch patch={game.patch} />
        </dl>
        {game.patch.check?.message && <p className="muted note">{game.patch.check.message}</p>}
        <div className="section-heading update-footer">
          <span className="muted"> {game.patch.check?.checked_at ?
            'Проверено ' + dateText(game.patch.check.checked_at) + ' · ' + game.patch.check.source : 'Проверка ещё не запускалась'}</span>
          {game.patch.history.length > 0 && <button className="text-button" aria-expanded={expanded === game.id}
            onClick={() => setExpanded(expanded === game.id ? null : game.id)}><History size={16} /> История</button>}
        </div>
        {expanded === game.id && <ul className="history-list">
          {game.patch.history.map(patch => <li key={patch.version + patch.source}>
            <strong className="mono">{patch.version}</strong><span title={patch.firmware ?? undefined}>
              {patch.firmware ? 'FW ' + patchFirmwareText(patch.firmware) : 'Прошивка неизвестна'}</span>
            <span>{patch.source}</span><span>{dateText(patch.checked_at)}</span>
          </li>)}
        </ul>}
      </section>)}
    </div>
  </>
}

function RootForm({ root, close, save }: { root: Root | null; close: () => void; save: () => Promise<void> }) {
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError('')
    const path = String(new FormData(event.currentTarget).get('path') || '')
    try {
      if (root) {
        await api('/roots/' + root.id, json('PUT', { path }))
      } else {
        const roots = path.split(/\r?\n/).map(line => line.trim()).filter(Boolean).map(path => ({ path }))
        if (!roots.length) throw new Error('Укажите хотя бы одну папку')
        await api('/roots/batch', json('POST', { roots }))
      }
      await save()
    } catch (failure) { setError((failure as Error).message) }
    finally { setBusy(false) }
  }
  return <Dialog title={root ? 'Переназначить папку' : 'Добавить папки'} close={busy ? () => {} : close}>
    <form onSubmit={submit}>
      <div className="dialog-body">
        {root
          ? <label className="field">Полный путь на компьютере с Prospero Library<input name="path" required autoFocus defaultValue={root.path}
            placeholder={'F:\\Games\\PS5'} maxLength={2048} /></label>
          : <label className="field">Полные пути, по одному на строку<textarea name="path" required autoFocus rows={4}
            placeholder={'D:\\Games\\PS5\nF:\\Games\\PS4'} /></label>}
        {error && <p className="error" role="alert">{error}</p>}
      </div>
      <div className="dialog-footer"><button type="button" className="button outlined" disabled={busy} onClick={close}>Отмена</button>
        <button className="button primary" disabled={busy}>Сохранить</button></div>
    </form>
  </Dialog>
}

type ArtworkProvider = 'IGDB' | 'SteamGridDB'

function ArtworkKeysDialog({ provider, close, save }: {
  provider: ArtworkProvider; close: () => void; save: (keys: Record<string, string>) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const keys = Object.fromEntries([...new FormData(event.currentTarget).entries()]
      .map(([key, value]) => [key, String(value).trim()]).filter(([, value]) => value))
    if (!Object.keys(keys).length) {
      setError('Введи хотя бы одно значение.')
      return
    }
    setBusy(true)
    setError('')
    try { await save(keys) }
    catch (failure) {
      setError((failure as Error).message)
      setBusy(false)
    }
  }
  return <Dialog title={'Настройки ' + provider} close={busy ? () => {} : close}>
    <form onSubmit={submit} autoComplete="off">
      <div className="dialog-body form-grid">
        {provider === 'IGDB' ? <>
          <p className="muted span-two">Для доступа укажи Client ID, а также Client Secret или Access Token.</p>
          <label className="field span-two">Client ID<input name="IGDB_CLIENT_ID" maxLength={2048} /></label>
          <label className="field span-two">Client Secret<input type="password" name="IGDB_CLIENT_SECRET" autoComplete="new-password" maxLength={2048} /></label>
          <label className="field span-two">Access Token<input type="password" name="IGDB_ACCESS_TOKEN" autoComplete="new-password" maxLength={2048} /></label>
        </> : <label className="field span-two">Ключ SteamGridDB
          <input type="password" name="STEAMGRIDDB_API_KEY" autoComplete="new-password" maxLength={2048} />
        </label>}
        <p className="muted span-two">{provider === 'IGDB'
          ? 'Пустые поля сохраняют прежние значения. При вводе нового Client Secret пустое поле Access Token сбрасывает старый токен.'
          : 'Чтобы заменить сохранённый ключ, введи новый.'}</p>
        {error && <p className="error span-two" role="alert">{error}</p>}
      </div>
      <div className="dialog-footer">
        <button type="button" className="button outlined" onClick={close} disabled={busy}>Отмена</button>
        <button className="button primary" disabled={busy}>Сохранить</button>
      </div>
    </form>
  </Dialog>
}

export function SettingsPage({ refresh, setHideRecent, scan, running, openJournal }: {
  refresh: () => Promise<void>; setHideRecent: (value: boolean) => void;
  scan: (force?: boolean) => void; running: boolean; openJournal: () => void;
}) {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [backups, setBackups] = useState<string[]>([])
  const [rootForm, setRootForm] = useState<Root | null | false>(false)
  const [restore, setRestore] = useState('')
  const [deleteBackup, setDeleteBackup] = useState('')
  const [backupError, setBackupError] = useState('')
  const [artworkProvider, setArtworkProvider] = useState<ArtworkProvider | null>(null)
  const [remove, setRemove] = useState<Root | null>(null)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [cli, setCli] = useState('')
  async function load() {
    const [configuration, files] = await Promise.all([api<Settings>('/settings'), api<string[]>('/backups')])
    setSettings(configuration)
    setBackups(files)
  }
  useEffect(() => { void load().catch(failure => setError(failure.message)) }, [])
  useEffect(() => {
    if (!running) void api<Settings>('/settings').then(setSettings).catch(() => {})
  }, [running])
  async function action(callback: () => Promise<void>, reportError = setError) {
    setBusy(true)
    setNotice('')
    setError('')
    try { await callback() }
    catch (failure) { reportError((failure as Error).message) }
    finally { setBusy(false) }
  }
  function update<K extends keyof Settings>(key: K, value: Settings[K]) {
    if (settings) setSettings({ ...settings, [key]: value })
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!settings) return
    const body = { cli_path: settings.cli_path, ps4_firmware: settings.ps4_firmware || null, ps5_firmware: settings.ps5_firmware || null }
    await action(async () => {
      setSettings(await api<Settings>('/settings', json('PUT', body)))
      await refresh()
      setNotice('Настройки сохранены')
    })
  }
  return <>
    <div className="page-intro"><h1>Настройки</h1><p>Папки коллекции, прошивки и резервные копии</p></div>
    {error && <p className="banner error" role="alert">{error}</p>}
    {notice && <p className="banner" role="status">{notice}</p>}
    {!settings && !error && <p role="status" className="muted">Загружаю настройки…</p>}
    {settings && <div className="management-stack">
      <section className="panel">
        <h2>Библиотека</h2>
        <label className="check-label preference-label"><input type="checkbox" checked={settings.hide_recent} disabled={busy}
          onChange={event => {
            const hideRecent = event.target.checked
            update('hide_recent', hideRecent)
            void action(async () => {
              try {
                const saved = await api<Settings>('/settings/library', json('PATCH', { hide_recent: hideRecent }))
                setHideRecent(saved.hide_recent)
              } catch (error) {
                setSettings(current => current && { ...current, hide_recent: !hideRecent })
                throw error
              }
            })
          }} />Скрывать недавно добавленную игру</label>
      </section>
      <section className="panel">
        <div className="section-heading"><h2>Папки коллекции <span className="count-badge">{settings.roots.length}</span></h2>
          <button className="button outlined compact" disabled={running || busy}
            onClick={() => setRootForm(null)}><Plus size={15} /> Добавить папки</button></div>
        <p className="muted">Prospero ищет PKG во всех указанных папках и их подпапках.</p>
        <ul className="roots-list">
          {settings.roots.map(root => <li key={root.id}><Folder size={17} /><span title={root.path}>{root.path}</span>
            <small>{({ unknown: 'Не проверена', offline: 'Недоступна', online: 'Доступна', partial: 'Частично доступна' })[root.state]}</small>
            <button className="text-button" disabled={busy || running} onClick={() => setRootForm(root)}>Переназначить</button>
            <button className="icon-button" disabled={busy || running} onClick={() => setRemove(root)} aria-label={'Убрать папку ' + root.path}><X size={16} /></button>
          </li>)}
        </ul>
        <div className="button-row">
          <button className="button outlined" disabled={running || busy || !settings.roots.length} onClick={() => scan()}>
            <ScanLine size={17} /> Сканировать все</button>
          <button className="button outlined" disabled={running || busy || !settings.roots.length} onClick={() => scan(true)}>
            <RotateCcw size={17} /> Перепроверить файлы</button>
        </div>
      </section>
      <section className="panel">
        <h2>Локальный PKG-парсер</h2>
        {settings.cli_local && <p className="positive cli-local">Активна копия из tools/fpkg-cli</p>}
        <form id="settings-form" onSubmit={submit} className="form-grid">
          <label className="field span-two">Путь к fpkg-cli или его папке установки<input value={settings.cli_path}
            onChange={event => update('cli_path', event.target.value)} maxLength={2048} /></label>
          <label className="field">Целевая прошивка PS5<input value={settings.ps5_firmware}
            onChange={event => update('ps5_firmware', event.target.value)} placeholder="13.60" /></label>
          <label className="field">Целевая прошивка PS4<input value={settings.ps4_firmware}
            onChange={event => update('ps4_firmware', event.target.value)} placeholder="Не задана" /></label>
          <div className="button-row journal-actions span-two">
            <button form="settings-form" className="button primary" disabled={busy}>Сохранить</button>
            <button type="button" className="button outlined" disabled={busy} onClick={() => void action(async () => {
              const result = await api<{ summary: string }>('/settings/cli', { method: 'POST' })
              setCli(result.summary)
            })}>Проверить CLI</button>
            {!settings.cli_local && <button type="button" className="button outlined" disabled={busy || running}
              onClick={() => void action(async () => {
                setSettings(await api<Settings>('/settings/cli/copy', { method: 'POST' }))
                setNotice('fpkg-cli скопирован в папку Prospero')
              })}>Скопировать CLI в Prospero</button>}
          </div>
        </form>
        {cli && <pre className="muted cli-output" role="status">{cli}</pre>}
      </section>
      <section className="panel">
        <h2>Источники оформления</h2>
        <ul className="provider-list">{Object.entries(settings.providers).map(([name, available]) => <li key={name}>
          <span>{name}</span>
          <span className="provider-actions">
            <small className={available ? 'positive' : 'muted'}>{available ? 'Настроен' : 'Ключ не задан'}</small>
            {(name === 'IGDB' || name === 'SteamGridDB') && <button type="button" className="text-button"
              disabled={busy} aria-label={(available ? 'Изменить' : 'Задать') + ' ключ ' + name}
              onClick={() => { setNotice(''); setArtworkProvider(name) }}>
              {available ? 'Изменить' : 'Задать'}
            </button>}
          </span>
        </li>)}</ul>
      </section>
      <section className="panel">
        <div className="section-heading"><h2>Резервные копии</h2>
          <button className="button outlined compact" disabled={busy} onClick={() => void action(async () => {
            const result = await api<{ name: string }>('/backups', { method: 'POST' })
            setBackups(items => [result.name, ...items])
            setNotice('Резервная копия создана')
          })}><Plus size={16} /> Создать копию</button></div>
        {!backups.length ? <p className="muted">Здесь появятся копии базы, настроек и изображений.</p> : <ul className="roots-list">
          {backups.map(name => <li key={name}>
            <History size={17} /><span>{name}</span>
            <a className="icon-button" href={'/api/backups/' + name} download title="Скачать архив" aria-label={'Скачать ' + name}><Download size={17} /></a>
            <button className="text-button" disabled={busy || running} onClick={() => setRestore(name)}>Восстановить</button>
            <button type="button" className="icon-button" disabled={busy} title="Удалить резервную копию"
              aria-label={'Удалить ' + name} onClick={() => { setBackupError(''); setDeleteBackup(name) }}><Trash2 size={17} /></button>
          </li>)}
        </ul>}
      </section>
      <section className="panel">
        <div className="section-heading">
          <h2>Журнал диагностики</h2>
          <button className="button outlined compact" onClick={openJournal}><FileText size={16} /> Открыть журнал</button>
        </div>
        <p className="muted">Результаты сканирования, проверки обновлений и ошибки приложения.</p>
      </section>
    </div>}
    {artworkProvider && <ArtworkKeysDialog provider={artworkProvider} close={() => setArtworkProvider(null)} save={async keys => {
      const { providers } = await api<Pick<Settings, 'providers'>>('/settings/artwork', json('PATCH', keys))
      setSettings(current => current && { ...current, providers })
      setArtworkProvider(null)
      setNotice('Ключи сохранены')
    }} />}
    {rootForm !== false && <RootForm root={rootForm} close={() => setRootForm(false)} save={async () => {
      await load()
      await refresh()
      setRootForm(false)
    }} />}
    {remove && <Dialog title="Убрать папку из коллекции" close={() => setRemove(null)}>
      <div className="dialog-body"><p className="notes">{remove.path}</p><p className="muted">Карточки останутся в библиотеке. Связь с файлами из этой папки будет удалена.</p></div>
      <div className="dialog-footer"><button className="button outlined" onClick={() => setRemove(null)}>Отмена</button>
        <button className="button primary" disabled={busy} onClick={() => void action(async () => {
          await api('/roots/' + remove.id, { method: 'DELETE' })
          setRemove(null)
          await load()
          await refresh()
        })}>Убрать папку</button></div>
    </Dialog>}
    {restore && <Dialog title="Восстановить резервную копию" close={() => setRestore('')}>
      <div className="dialog-body"><p className="notes">{restore}</p><p className="muted">Текущая библиотека будет заменена. Перед восстановлением создастся защитная копия.</p></div>
      <div className="dialog-footer"><button className="button outlined" onClick={() => setRestore('')}>Отмена</button>
        <button className="button primary" disabled={busy} onClick={() => void action(async () => {
          await api('/backups/restore', json('POST', { name: restore }))
          setRestore('')
          await load()
          await refresh()
          setNotice('Библиотека восстановлена')
        })}>Восстановить</button></div>
    </Dialog>}
    {deleteBackup && <Dialog title="Удалить резервную копию" close={busy ? () => {} : () => setDeleteBackup('')}>
      <div className="dialog-body">
        <p className="muted">Этот архив будет удалён с диска.</p>
        <p className="notes">{deleteBackup}</p>
        {backupError && <p className="error" role="alert">{backupError}</p>}
      </div>
      <div className="dialog-footer">
        <button className="button outlined" disabled={busy} onClick={() => setDeleteBackup('')}>Отмена</button>
        <button className="button primary" disabled={busy} onClick={() => {
          setBackupError('')
          void action(async () => {
            await api('/backups/' + encodeURIComponent(deleteBackup), { method: 'DELETE' })
            setBackups(items => items.filter(name => name !== deleteBackup))
            setDeleteBackup('')
            setNotice('Резервная копия удалена')
          }, setBackupError)
        }}>Удалить</button>
      </div>
    </Dialog>}
  </>
}

export function JobDialog({ job, close, cancel, openJournal }: {
  job: Job; close: () => void; cancel: () => void; openJournal: () => void;
}) {
  const states = { running: 'Выполняется', cancelling: 'Отменяю…', done: 'Завершено', cancelled: 'Отменено',
    error: 'Не удалось завершить', interrupted: 'Прервано' }
  const status = job.status === 'done' && job.report.errors ? 'Завершено с ошибками' : states[job.status]
  const scan = job.kind === 'scan'
  return <Dialog title={scan ? 'Сканирование коллекции' : 'Проверка обновлений'} close={close}>
    <div className="dialog-body">
      <p role="status" className="job-state">{status}{job.report.current ? ' · ' + job.report.current : ''}</p>
      <dl className="meta-grid">
        <div><dt>{scan ? 'Найдено' : 'Обработано'}</dt><dd>{job.report.found}</dd></div>
        {scan && <div><dt>Добавлено</dt><dd>{job.report.added}</dd></div>}
        <div><dt>{scan ? 'Обновлено' : 'Данные получены'}</dt><dd>{job.report.updated}</dd></div>
        <div><dt>{scan ? 'Пропущено' : 'Нет данных'}</dt><dd>{job.report.skipped}</dd></div>
        <div><dt>Ошибки</dt><dd>{job.report.errors}</dd></div>
      </dl>
      {job.report.issues.length > 0 && <ul className="job-issues">{job.report.issues.map((issue, i) => <li key={i}>{issue}</li>)}</ul>}
    </div>
    <div className="dialog-footer journal-actions">
      <button className="button outlined" onClick={openJournal}><FileText size={16} /> Журнал</button>
      {job.status === 'running' && <button className="button outlined" onClick={cancel}>Отменить операцию</button>}
      <button className="button primary" onClick={close}>Закрыть</button>
    </div>
  </Dialog>
}

export function JournalDialog({ close }: { close: () => void }) {
  const [text, setText] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [pending, setPending] = useState<'read' | 'clear' | null>('read')
  const log = useRef<HTMLTextAreaElement>(null)

  async function load() {
    setPending('read')
    setError('')
    setNotice('')
    try {
      setText((await api<{ text: string }>('/logs')).text)
    } catch (failure) {
      setError((failure as Error).message)
    } finally {
      setPending(null)
    }
  }

  useEffect(() => { void load() }, [])
  useEffect(() => {
    if (log.current) log.current.scrollTop = log.current.scrollHeight
  }, [text])

  async function clear() {
    if (!window.confirm('Удалить все записи из журнала диагностики?')) return
    setPending('clear')
    setError('')
    setNotice('')
    try {
      setText((await api<{ text: string }>('/logs', { method: 'DELETE' })).text)
      setNotice('Журнал очищен')
    } catch (failure) {
      setError((failure as Error).message)
    } finally {
      setPending(null)
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setNotice('Журнал скопирован')
    } catch {
      log.current?.focus()
      log.current?.select()
      setNotice('Текст выделен. Нажми Ctrl+C.')
    }
  }

  return <Dialog title="Журнал диагностики" close={close} wide>
    <div className="dialog-body">
      {error && <p role="alert" className="error">{error}</p>}
      {pending && <p role="status" className="muted">{pending === 'clear' ? 'Очищаю журнал…' : 'Загружаю журнал…'}</p>}
      {!pending && !error && !notice && !text && <p className="muted">Журнал пока пуст.</p>}
      {text && <textarea className="journal-text mono" aria-label="Записи журнала" ref={log} value={text} readOnly spellCheck={false} />}
      {notice && <p className="muted" role="status">{notice}</p>}
    </div>
    <div className="dialog-footer journal-actions">
      <button className="button outlined" disabled={pending !== null} onClick={() => void load()}><RefreshCw size={16} /> Обновить</button>
      <button className="button outlined" disabled={!text || pending !== null} onClick={() => void clear()}><Trash2 size={16} /> Очистить журнал</button>
      <button className="button outlined" disabled={!text || pending !== null} onClick={() => void copy()}><Clipboard size={16} /> Скопировать</button>
      <a className="button primary" href="/api/logs?download=true" download="prospero-log.txt"><Download size={16} /> Скачать TXT</a>
    </div>
  </Dialog>
}
