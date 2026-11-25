import { useCallback, useEffect, useRef, useState } from 'react'
import { Activity, Archive, ChevronRight, CircleAlert, Gamepad2, Grid2X2, Plus, RefreshCw, ScanLine, Search, Settings2, X } from 'lucide-react'
import { ArtworkDialog, GameEditor, GamePage } from './Game'
import { Library } from './Library'
import { JobDialog, JournalDialog, SettingsPage, Updates } from './Management'
import { api, json } from './model'
import type { Game, Job, Settings, StoreLocalization } from './model'

function active(job: Job | null) {
  return job?.status === 'running' || job?.status === 'cancelling'
}
const navigation = [
  { route: '/', label: 'Библиотека', icon: Grid2X2 },
  { route: '/updates', label: 'Обновления', icon: RefreshCw },
  { route: '/settings', label: 'Настройки', icon: Settings2 },
]

export function App() {
  const [route, setRoute] = useState(window.location.pathname)
  const [games, setGames] = useState<Game[]>([])
  const [ready, setReady] = useState(false)
  const [hideRecent, setHideRecent] = useState(false)
  const [query, setQuery] = useState('')
  const [job, setJob] = useState<Job | null>(null)
  const [jobOpen, setJobOpen] = useState(new URLSearchParams(window.location.search).has('job'))
  const [journalOpen, setJournalOpen] = useState(false)
  const [mode, setMode] = useState<'new' | 'edit' | 'art' | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [starting, setStarting] = useState(false)
  const input = useRef<HTMLInputElement>(null)

  const refresh = useCallback(async () => {
    const [items, settings] = await Promise.all([api<Game[]>('/games'), api<Settings>('/settings').catch(() => null)])
    setGames(items)
    if (settings) setHideRecent(settings.hide_recent)
    setReady(true)
  }, [])
  const saveLocalization = useCallback((id: number, contentId: string, localization: StoreLocalization) => {
    setGames(items => items.map(game => game.id === id && game.content_id === contentId
      ? { ...game, official_localization: localization } : game))
  }, [])

  const navigate = useCallback((path: string, clear = false) => {
    if (clear) setQuery('')
    if (window.location.pathname !== path) {
      window.history.pushState(null, '', path)
      setRoute(path)
      window.scrollTo({ top: 0, behavior: 'instant' })
    }
  }, [])

  function showError(failure: unknown) { setError((failure as Error).message) }
  useEffect(() => {
    const back = () => { setRoute(window.location.pathname); setMode(null) }
    const key = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k' && !document.querySelector('dialog[open]')) {
        event.preventDefault()
        input.current?.focus()
      }
    }
    window.addEventListener('popstate', back)
    window.addEventListener('keydown', key)
    void refresh().catch(showError)
    void api<Job | null>('/jobs/latest').then(setJob).catch(() => {})
    return () => {
      window.removeEventListener('popstate', back)
      window.removeEventListener('keydown', key)
    }
  }, [refresh])

  useEffect(() => {
    if (!active(job) || !job) return
    let mounted = true
    const timer = window.setTimeout(() => {
      void api<Job>('/jobs/' + job.id).then(result => {
        if (!mounted) return
        setJob(result)
        if (!active(result)) void refresh().catch(showError)
      }).catch(failure => { if (mounted) { showError(failure); setJob(null) } })
    }, 700)
    return () => { mounted = false; clearTimeout(timer) }
  }, [job, refresh])

  useEffect(() => {
    if (!notice) return
    const timeout = setTimeout(() => setNotice(''), 3600)
    return () => clearTimeout(timeout)
  }, [notice])

  async function start(path: string, body?: unknown) {
    if (active(job) || starting) { setJobOpen(true); return }
    setStarting(true)
    setError('')
    try {
      setJob(await api<Job>(path, body ? json('POST', body) : { method: 'POST' }))
      setJobOpen(true)
    } catch (failure) { showError(failure) }
    finally { setStarting(false) }
  }

  const detail = /^\/games\/(\d+)$/.exec(route)
  const current = detail ? games.find(game => game.id === Number(detail[1])) : undefined
  const home = route === '/'
  const updating = active(job) || starting
  const showGame = (id: number) => navigate('/games/' + id)
  async function swapGames(source: number, target: number) {
    setError('')
    try {
      const ordered = await api<number[]>('/games/order', json('POST', { source_id: source, target_id: target }))
      const positions = new Map(ordered.map((id, index) => [id, index]))
      setGames(items => items.toSorted((a, b) => (positions.get(a.id) ?? ordered.length) - (positions.get(b.id) ?? ordered.length)))
    } catch (failure) { showError(failure) }
  }
  function saveGame(game: Game) {
    setGames(items => items.some(item => item.id === game.id) ? items.map(item => item.id === game.id ? game : item) : [...items, game])
    if (mode !== 'art') setMode(null)
    showGame(game.id)
    setNotice('Сохранено')
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <a className="brand" href="/" onClick={event => { event.preventDefault(); navigate('/', true) }}>
        <span className="brand-mark" aria-hidden="true"><Archive size={22} strokeWidth={1.9} /></span>
        <span><strong>Prospero</strong><small>LIBRARY</small></span>
      </a>
      <nav aria-label="Основное меню" className="navigation">
        {navigation.map(({ route: path, label, icon: Icon }) => {
          const selected = route === path || (Boolean(detail) && path === '/')
          return <a key={path} href={path} aria-label={label} aria-current={selected ? 'page' : undefined}
            onClick={event => { event.preventDefault(); navigate(path, true) }}>
            <Icon size={18} strokeWidth={1.75} /><span>{label}</span>{selected && <ChevronRight className="nav-chevron" size={15} />}
          </a>
        })}
      </nav>
    </aside>
    <div className="app-main">
      <header className="topbar">
        <label className="searchbox"><Search size={18} />
          <input value={query} ref={input} type="text" placeholder="Название или Title ID" aria-label="Поиск по библиотеке"
            onChange={event => { setQuery(event.target.value); navigate('/') }}
            onFocus={() => { if (!home && query) navigate('/') }} />
          {query ? <button className="icon-button" onClick={() => { setQuery(''); input.current?.focus() }} aria-label="Очистить поиск"><X size={15} /></button> : <kbd>Ctrl K</kbd>}
        </label>
        <div className="top-actions">
          {job && <button className={'icon-button job-button ' + (active(job) ? 'working' : '')} aria-label="Ход операции" title="Ход операции" onClick={() => setJobOpen(true)}><Activity size={19} /></button>}
          <button className="button outlined top-scan" aria-label="Сканировать" title="Сканировать коллекцию" disabled={updating} onClick={() => void start('/scan', { force: false })}><ScanLine size={18} /> <span>Сканировать</span></button>
          <button className="button primary" aria-label="Добавить игру" title="Добавить игру" onClick={() => setMode('new')}><Plus size={19} /> <span>Добавить игру</span></button>
        </div>
      </header>
      <main className="content">
        {error && <div className="banner error" role="alert"><CircleAlert size={18} /><span>{error}</span>
          {!ready && <button className="text-button" onClick={() => { setError(''); void refresh().catch(showError) }}>Повторить</button>}
          {ready && <button className="text-button" onClick={() => setJournalOpen(true)}>Журнал</button>}
          <button className="icon-button" aria-label="Закрыть уведомление" onClick={() => setError('')}><X size={17} /></button></div>}
        {!ready && !error && <p className="muted" role="status">Загружаю библиотеку…</p>}
        {ready && home && <Library games={games} hideRecent={hideRecent} query={query}
          open={game => showGame(game.id)} add={() => setMode('new')} swap={swapGames} />}
        {ready && current && <GamePage key={current.id} game={current} back={() => navigate('/')} edit={() => setMode('edit')}
          onLocalization={saveLocalization}
          art={() => setMode('art')} updating={updating} updates={() => void start('/updates?game_id=' + current.id)}
          removed={() => { setGames(items => items.filter(game => game.id !== current.id)); navigate('/', true); setNotice('Карточка удалена') }} />}
        {ready && detail && !current && <div className="empty-state"><Gamepad2 size={28} /><p>Карточка не найдена</p>
          <button className="button outlined" onClick={() => navigate('/')}>К библиотеке</button></div>}
        {ready && route === '/updates' && <Updates games={games} open={showGame} busy={updating}
          start={id => void start('/updates' + (id == null ? '' : '?game_id=' + id))} />}
        {route === '/settings' && <SettingsPage refresh={refresh} setHideRecent={setHideRecent} running={updating} scan={force => void start('/scan', { force: !!force })}
          openJournal={() => setJournalOpen(true)} />}
      </main>
    </div>
    {notice && <div className="toast" role="status">{notice}</div>}
    {mode === 'new' && <GameEditor close={() => setMode(null)} save={saveGame} />}
    {mode === 'edit' && current && <GameEditor game={current} close={() => setMode(null)} save={saveGame} />}
    {mode === 'art' && current && <ArtworkDialog game={current} close={() => setMode(null)} save={saveGame} />}
    {jobOpen && job && <JobDialog job={job} close={() => setJobOpen(false)} openJournal={() => setJournalOpen(true)} cancel={() =>
      void api<Job>('/jobs/' + job.id + '/cancel', { method: 'POST' }).then(setJob).catch(showError)} />}
    {journalOpen && <JournalDialog close={() => setJournalOpen(false)} />}
  </div>
}
