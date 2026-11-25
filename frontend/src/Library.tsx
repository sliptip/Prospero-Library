import { useEffect, useState } from 'react'
import { ArrowDownUp, ArrowRight, ChevronDown, Gamepad2, Grid2X2, GripVertical, LibraryBig, List, Plus, SlidersHorizontal, X } from 'lucide-react'
import { Game, gameCount, sizeText, stored } from './model'

type Props = { games: Game[]; query: string; open: (game: Game) => void; add: () => void; swap: (source: number, target: number) => Promise<void> }
type Sort = 'added_at' | 'title' | 'release_date' | 'size_bytes' | 'update' | 'manual'
type Filters = { dlc: boolean; ru_text: boolean; ru_audio: boolean; update: boolean }
const defaults = { dlc: false, ru_text: false, ru_audio: false, update: false }
const filtersList: [keyof Filters, string][] = [
  ['dlc', 'С дополнениями'], ['ru_text', 'Русский текст'], ['ru_audio', 'Русская озвучка'], ['update', 'Есть обновление'],
]
const order: [Sort, string][] = [
  ['added_at', 'По добавлению'], ['title', 'По названию'], ['release_date', 'По дате выхода'],
  ['size_bytes', 'По размеру'], ['update', 'По обновлениям'], ['manual', 'Ручной порядок'],
]
const collator = new Intl.Collator(['ru', 'en'], { sensitivity: 'base', numeric: true })

function matchText(value: string) {
  return value.normalize('NFKC').toLocaleLowerCase('ru').replaceAll('ё', 'е')
}

export function Cover({ game, className = '' }: { game: Game; className?: string }) {
  return <div className={'cover ' + className}>
    {game.cover ? <img src={game.cover} alt="" loading="lazy" /> : <Gamepad2 size={36} aria-hidden="true" />}
  </div>
}

function Hero({ game, open }: { game: Game; open: () => void }) {
  const firstColon = game.title.indexOf(':')
  return <section className="hero" aria-label="Недавно добавлено">
    {game.background && <img className="hero-art" src={game.background} alt="" fetchPriority="high" />}
    <div className="hero-gradient" />
    <div className="hero-content">
      <div className="eyebrow"><span /> НЕДАВНО ДОБАВЛЕНО</div>
      <h2>{firstColon > 0 ? <>{game.title.slice(0, firstColon + 1)}<br />{game.title.slice(firstColon + 1).trim()}</> : game.title}</h2>
      <div className="hero-meta"><span className="platform">{game.platform}</span>{game.edition && <span>{game.edition}</span>}</div>
      <button className="button hero-button" onClick={open}>Открыть карточку <ArrowRight size={16} /></button>
    </div>
  </section>
}

export function Library({ games, query, open, add, swap }: Props) {
  const [platform, setPlatform] = useState(() => stored<'all' | 'PS5' | 'PS4'>('prospero-platform', 'all'))
  const [mode, setMode] = useState(() => stored<'grid' | 'list'>('prospero-mode', 'grid'))
  const [sort, setSort] = useState(() => stored<Sort>('prospero-sort', 'added_at'))
  const [asc, setAsc] = useState(() => stored('prospero-ascending', false))
  const [filters, setFilters] = useState(() => ({ ...defaults, ...stored<Partial<Filters>>('prospero-filters', {}) }))
  const [filterOpen, setFilterOpen] = useState(false)
  const [selected, setSelected] = useState<number | null>(null)
  const [dragging, setDragging] = useState<number | null>(null)
  const [dropTarget, setDropTarget] = useState<number | null>(null)
  const [savingOrder, setSavingOrder] = useState(false)

  useEffect(() => {
    const options = { platform, mode, sort, ascending: asc, filters }
    for (const [key, value] of Object.entries(options)) localStorage.setItem('prospero-' + key, JSON.stringify(value))
  }, [platform, mode, sort, asc, filters])

  const search = matchText(query.trim())
  const recent = games.toSorted((a, b) => b.added_at.localeCompare(a.added_at))[0]
  const activeFilters = Object.values(filters).filter(Boolean).length
  const filtered = games.filter(game => (
    (!search || matchText(game.title + ' ' + game.title_id).includes(search)) &&
    (platform === 'all' || game.platform === platform) &&
    (!filters.dlc || game.dlc.some(item => item.status === 'verified' || item.status === 'configured')) &&
    (!filters.ru_text || game.ru_text) && (!filters.ru_audio || game.ru_audio) &&
    (!filters.update || game.patch.state === 'available')
  )).sort((a, b) => {
    if (sort === 'manual') return 0
    const lhs = sort === 'update' ? (a.patch.state === 'available' ? 1 : a.patch.state === 'current' ? 0 : null) : a[sort]
    const rhs = sort === 'update' ? (b.patch.state === 'available' ? 1 : b.patch.state === 'current' ? 0 : null) : b[sort]
    if (lhs == null || rhs == null) return (lhs == null ? 1 : 0) - (rhs == null ? 1 : 0) || collator.compare(a.title, b.title)
    const result = typeof lhs === 'number' && typeof rhs === 'number' ? lhs - rhs : collator.compare(String(lhs), String(rhs))
    return result * (asc ? 1 : -1) || collator.compare(a.title, b.title)
  })
  const count = (group: 'all' | 'PS5' | 'PS4') => group === 'all' ? games.length : games.filter(game => game.platform === group).length
  const manual = sort === 'manual' && filtered.length > 1
  const selectedGame = manual ? filtered.find(game => game.id === selected) : undefined

  async function exchange(source: number, target: number) {
    setSelected(null)
    setDragging(null)
    setDropTarget(null)
    if (source === target || savingOrder) return
    setSavingOrder(true)
    try { await swap(source, target) }
    finally { setSavingOrder(false) }
  }

  return <>
    <div className="page-intro">
      <h1>Моя библиотека</h1>
      <p>{gameCount(games.length)} в коллекции</p>
    </div>
    {recent && !search && <Hero game={recent} open={() => open(recent)} />}
    <section className="collection" aria-labelledby="collection-heading">
      <div className="section-heading">
        <div className="section-title"><h2 id="collection-heading">Коллекция</h2><span className="count-badge">{filtered.length}</span></div>
        <div className="section-tools">
          <div className="sort-control">
            <button className="icon-button sort-direction" disabled={sort === 'manual'} title={asc ? 'По возрастанию' : 'По убыванию'} aria-label={asc ? 'По возрастанию' : 'По убыванию'} onClick={() => setAsc(!asc)}>
              <ArrowDownUp size={16} />
            </button>
            <label className="sort-select"><span className="sr-only">Сортировка</span>
              <select value={sort} onChange={event => {
                const selected = event.target.value as Sort
                setSort(selected)
                setSelected(null)
                setAsc(selected === 'title')
              }}>{order.map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select>
              <ChevronDown size={13} />
            </label>
          </div>
          <div className="filter-wrap">
            <button className={'button outlined compact ' + (activeFilters ? 'accent-text' : '')} aria-expanded={filterOpen} onClick={() => setFilterOpen(!filterOpen)}>
              <SlidersHorizontal size={16} /> <span>Фильтры{activeFilters ? ' · ' + activeFilters : ''}</span>
            </button>
            {filterOpen && <div className="filter-panel" onKeyDown={event => { if (event.key === 'Escape') setFilterOpen(false) }}>
              <div className="filter-heading"><strong>Фильтры</strong><button className="icon-button" aria-label="Закрыть фильтры" onClick={() => setFilterOpen(false)}><X size={15} /></button></div>
              {filtersList.map(([id, label]) => <label key={id} className="check-label">
                <input type="checkbox" checked={filters[id]} onChange={event => setFilters({ ...filters, [id]: event.target.checked })} />{label}
              </label>)}
              <button className="text-button" onClick={() => setFilters(defaults)}>Сбросить</button>
            </div>}
          </div>
          <div className="mode-group" aria-label="Вид коллекции">
            <button title="Шкаф" aria-label="Шкаф" aria-pressed={mode === 'grid'} onClick={() => setMode('grid')}><Grid2X2 size={16} /></button>
            <button title="Список" aria-label="Список" aria-pressed={mode === 'list'} onClick={() => setMode('list')}><List size={18} /></button>
          </div>
        </div>
      </div>
      {manual && <div className="reorder-hint" role="status">
        {selectedGame ? <><span>Выбери карточку, чтобы поменять её местами с «{selectedGame.title}».</span>
          <button className="text-button" onClick={() => setSelected(null)}>Отмена</button></>
          : <span>Перетащи карточку за ручку или нажми на ручку и выбери вторую карточку.</span>}
      </div>}
      <div className="platform-tabs" aria-label="Платформа">
        {(['all', 'PS5', 'PS4'] as const).map(id => <button key={id} aria-pressed={platform === id} onClick={() => setPlatform(id)}>
          {id === 'all' ? 'Все игры' : id} <span>{count(id)}</span>
        </button>)}
      </div>
      {!filtered.length ? <div className="empty-state">
        <LibraryBig size={28} />
        <strong>{games.length ? 'Игры не найдены' : 'Шкаф пока пуст'}</strong>
        <p>{games.length ? 'Измените поиск или фильтры.' : 'Добавьте игру вручную или укажите папку коллекции в настройках.'}</p>
        {!games.length && <button className="button primary" onClick={add}><Plus size={17} /> Добавить игру</button>}
      </div> : <div className={mode === 'grid' ? 'shelf' : 'game-list'}>
        {filtered.map(game => <div key={game.id} className={'game-item' + (manual ? ' reorderable' : '') +
          (dropTarget === game.id || selectedGame?.id === game.id ? ' reorder-target' : '') +
          (dragging === game.id ? ' reordering' : '')}
          onDragOver={event => {
            if (dragging === null || dragging === game.id) return
            event.preventDefault()
            event.dataTransfer.dropEffect = 'move'
            setDropTarget(game.id)
          }}
          onDragLeave={event => {
            if (dropTarget === game.id && !event.currentTarget.contains(event.relatedTarget as Node | null)) setDropTarget(null)
          }}
          onDrop={event => {
            if (dragging === null) return
            event.preventDefault()
            void exchange(dragging, game.id)
          }}>
          <button className={mode === 'grid' ? 'game-tile' : 'game-row'} onClick={() => {
            if (selectedGame) void exchange(selectedGame.id, game.id)
            else open(game)
          }}>
            <div className="tile-art">
              <Cover game={game} />
              {mode === 'grid' && <span className="platform tile-platform">{game.platform}</span>}
              {mode === 'grid' && game.patch.state === 'available' && <span className="patch-dot" title="Есть обновление" />}
            </div>
            <div className="tile-text">
              <strong>{game.title}</strong>
              {mode === 'list' ? <span>{game.edition || game.title_id}</span> : <span className="tile-meta"><span>{game.version || '—'}</span>
                {game.dlc.some(dlc => dlc.status === 'configured' || dlc.status === 'verified') && <span>{game.dlc.filter(dlc => dlc.status === 'configured' || dlc.status === 'verified').length} DLC</span>}</span>}
            </div>
            {mode === 'list' && <><span className="platform">{game.platform}</span><span className="mono row-version">{game.version || '—'}</span>
              <span className="row-size">{sizeText(game.size_bytes)}</span><ArrowRight size={17} className="row-arrow" /></>}
          </button>
          {manual && <button type="button" className="icon-button reorder-handle" draggable={!savingOrder}
            disabled={savingOrder} title="Поменять местами" aria-label={'Поменять местами · ' + game.title}
            aria-pressed={selectedGame?.id === game.id} onClick={() => setSelected(selected === game.id ? null : game.id)}
            onDragStart={event => {
              event.dataTransfer.setData('text/plain', String(game.id))
              event.dataTransfer.effectAllowed = 'move'
              setSelected(null)
              setDragging(game.id)
            }}
            onDragEnd={() => { setDragging(null); setDropTarget(null) }}><GripVertical size={17} /></button>}
        </div>)}
        {mode === 'grid' && !search && platform === 'all' && !activeFilters && <button className="add-tile" onClick={add}><Plus size={22} />Добавить игру</button>}
      </div>}
    </section>
  </>
}
