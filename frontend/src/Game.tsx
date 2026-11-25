import { useEffect, useRef, useState } from 'react'
import type { ChangeEvent, FormEvent, ReactNode } from 'react'
import { ArrowLeft, Box, Download, ExternalLink, ImagePlus, Info, Pencil, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import { Cover } from './Library'
import { api, compatiblePatchText, dateText, dlcLabel, fileLabel, json, localText, patchArchive, patchFirmwareText, patchLabel, sizeText, unknown } from './model'
import type { ArtCandidate, Dlc, Game, GameAbout, PatchInfo, Platform } from './model'

export function Dialog({ title, close, children, wide = false }: { title: string; close: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current!
    dialog.showModal()
    return () => { if (dialog.open) dialog.close() }
  }, [])
  return <dialog ref={ref} className={wide ? 'dialog wide-dialog' : 'dialog'} aria-label={title}
    onCancel={event => { event.preventDefault(); close() }}>
    <div className="dialog-header"><h2>{title}</h2><button className="icon-button" aria-label="Закрыть" onClick={close}><X size={21} /></button></div>
    {children}
  </dialog>
}

const blankDlc: Dlc = { name: '', description: null, content_id: null, status: 'unknown' }
const enumBool = (value?: boolean | null) => value === true ? 'yes' : value === false ? 'no' : ''

export function GameEditor({ game, close, save }: { game?: Game; close: () => void; save: (game: Game) => void }) {
  const [platform, setPlatform] = useState<Platform>(game?.platform || 'PS5')
  const [editPackage, setEditPackage] = useState(false)
  const savedFields = {
    title: game?.title ?? '', title_id: game?.title_id ?? '', version: game?.version ?? '',
    firmware: game?.firmware ?? '', size_bytes: String(game?.size_bytes ?? ''),
  }
  const [fields, setFields] = useState(savedFields)
  const [dlc, setDlc] = useState<Dlc[]>(game?.dlc || [])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const locked = (name: string) => !editPackage && !!game?.package_fields.includes(name)
  const shownPlatform = game && locked('platform') ? game.platform : platform
  const input = (name: keyof typeof fields) => ({
    name,
    value: locked(name) ? savedFields[name] : fields[name],
    readOnly: locked(name),
    onChange: (event: ChangeEvent<HTMLInputElement>) => setFields({ ...fields, [name]: event.target.value }),
  })

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    setBusy(true)
    const data = new FormData(event.currentTarget)
    const value = (key: string) => String(data.get(key) || '').trim() || null
    const bool = (key: string) => value(key) === null ? null : value(key) === 'yes'
    const body = {
      title: value('title'), platform: shownPlatform, title_id: value('title_id')?.toUpperCase(),
      edit_pkg_fields: game?.package_identity ? editPackage : undefined,
      version: value('version'), firmware: value('firmware'), edition: value('edition'),
      size_bytes: value('size_bytes') ? Number(value('size_bytes')) : null, release_date: value('release_date'),
      ru_text: bool('ru_text'), ru_audio: bool('ru_audio'), text_languages: value('text_languages'),
      audio_languages: value('audio_languages'), localization_source: value('localization_source'),
      notes: value('notes'),
      dlc: dlc.map(item => ({ ...item, name: item.name.trim(), description: item.description?.trim() || null })),
    }
    try { save(await api<Game>(game ? '/games/' + game.id : '/games', json(game ? 'PUT' : 'POST', body))) }
    catch (failure) { setError(String((failure as Error).message)) }
    finally { setBusy(false) }
  }
  return <Dialog title={game ? 'Редактировать карточку' : 'Добавить игру'} close={busy ? () => {} : close} wide>
    <form onSubmit={submit}>
      <div className="dialog-body form-grid">
        {game?.package_identity && <div className="span-two package-editor">
          <label className="check-label"><input type="checkbox" checked={editPackage}
            onChange={event => setEditPackage(event.target.checked)} />Разрешить редактирование полей из PKG</label>
          {editPackage && <p className="muted">Для сканирования и проверки патчей используется Title ID из PKG — {game.package_identity.title_id}.</p>}
        </div>}
        <label className="field span-two">Название<input {...input('title')} required maxLength={256} /></label>
        <label className="field">Платформа<select value={shownPlatform} disabled={!!game && (!game.package_identity || locked('platform'))}
          onChange={event => setPlatform(event.target.value as Platform)}>
          <option>PS5</option><option>PS4</option>
        </select></label>
        <label className="field">Title ID<input {...input('title_id')} required readOnly={!!game && (!game.package_identity || locked('title_id'))}
          minLength={9} maxLength={9} placeholder={shownPlatform === 'PS5' ? 'PPSA00000' : 'CUSA00000'} /></label>
        <label className="field">Издание<input name="edition" maxLength={256} defaultValue={game?.edition || ''} /></label>
        <label className="field">Дата выхода<input type="date" name="release_date" defaultValue={game?.release_date || ''} /></label>
        <label className="field">Версия в коллекции<input {...input('version')} placeholder={shownPlatform === 'PS5' ? '01.000.000' : '01.00'} /></label>
        <label className="field">Требуемая прошивка игры<input {...input('firmware')} placeholder="13.40" /></label>
        <label className="field">Размер в байтах<input {...input('size_bytes')} type="number" min="1" step="1" /></label>
        <span />
        {(['ru_text', 'ru_audio'] as const).map((key, index) => <label key={key} className="field">{index === 0 ? 'Русский текст' : 'Русская озвучка'}
          <select name={key} defaultValue={enumBool(game?.[key])}><option value="">Не указано</option><option value="yes">Есть</option><option value="no">Нет</option></select>
        </label>)}
        <label className="field">Языки интерфейса и субтитров<input name="text_languages" maxLength={512} defaultValue={game?.text_languages || ''} /></label>
        <label className="field">Языки озвучки<input name="audio_languages" maxLength={512} defaultValue={game?.audio_languages || ''} /></label>
        <label className="field span-two">Источник русификации<input name="localization_source" maxLength={512} defaultValue={game?.localization_source || ''} /></label>
        <label className="field span-two">Примечания<textarea name="notes" rows={3} maxLength={12000} defaultValue={game?.notes || ''} /></label>
        <div className="span-two dlc-editor">
          <div className="section-heading"><h3>Дополнения</h3><button className="button outlined compact" type="button" onClick={() => setDlc([...dlc, { ...blankDlc }])} disabled={dlc.length >= 100}><Plus size={15} /> Добавить DLC</button></div>
          {dlc.map((item, index) => <div className="dlc-form" key={index}>
            <label className="field">Название DLC<input value={item.name} required maxLength={256}
              onChange={event => setDlc(dlc.map((other, i) => i === index ? { ...other, name: event.target.value } : other))} /></label>
            <label className="field">Статус DLC<select value={item.status} onChange={event => setDlc(dlc.map((other, i) =>
              i === index ? { ...other, status: event.target.value as Dlc['status'] } : other))}>
              {Object.entries(dlcLabel).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select></label>
            <button className="icon-button" type="button" title="Удалить строку DLC" aria-label="Удалить строку DLC"
              onClick={() => setDlc(dlc.filter((_, i) => i !== index))}><X size={18} /></button>
            <label className="field span-two">Описание<textarea rows={2} maxLength={1000} value={item.description || ''}
              onChange={event => setDlc(dlc.map((other, i) => i === index ? { ...other, description: event.target.value } : other))} /></label>
          </div>)}
        </div>
        {error && <p className="error span-two" role="alert">{error}</p>}
      </div>
      <div className="dialog-footer"><button type="button" disabled={busy} className="button outlined" onClick={close}>Отмена</button>
        <button className="button primary" disabled={busy}>{busy ? 'Сохраняю…' : 'Сохранить'}</button></div>
    </form>
  </Dialog>
}

function Meta({ name, children, className = '', title }: { name: string; children: ReactNode; className?: string; title?: string }) {
  return <div><dt>{name}</dt><dd className={className} title={title}>{children || unknown}</dd></div>
}

export function CompatiblePatch({ patch }: { patch: PatchInfo }) {
  const text = compatiblePatchText(patch)
  return text && <Meta name={'Совместимый патч · ' + patch.target} className="mono">{text}</Meta>
}

type AboutState = { status: 'loading' } | { status: 'ready'; game: GameAbout | null } | { status: 'error'; message: string }

function AboutGameDialog({ about, close, retry }: { about: AboutState; close: () => void; retry: () => void }) {
  return <Dialog title="Об игре" close={close} wide>
    <div className="dialog-body about-game-body">
      {about.status === 'loading' && <p className="muted" role="status">Загружаю описание…</p>}
      {about.status === 'error' && <div className="section-heading">
        <p className="muted" role="status">{about.message}</p>
        <button className="text-button" onClick={retry}>Повторить</button>
      </div>}
      {about.status === 'ready' && <>
        {about.game?.description ? <>
          {about.game.language === 'en' && <p className="muted about-game-language">Описание на английском</p>}
          <p className="about-game-description" lang={about.game.language}>{about.game.description}</p>
        </> : <p className="muted">В PlayStation Store нет описания для этой игры.</p>}
        {about.game?.url?.startsWith('https://store.playstation.com/') && <a href={about.game.url} target="_blank" rel="noreferrer" className="text-button note">
          PlayStation Store <ExternalLink size={13} />
        </a>}
      </>}
    </div>
  </Dialog>
}

export function GamePage({ game, back, edit, art, updates, updating, removed }: {
  game: Game; back: () => void; edit: () => void; art: () => void; updates: () => void; updating: boolean; removed: () => void;
}) {
  const [remove, setRemove] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [showAbout, setShowAbout] = useState(false)
  const [about, setAbout] = useState<AboutState>({ status: 'loading' })
  const [aboutAttempt, setAboutAttempt] = useState(0)
  const store = about.status === 'ready' ? about.game : null
  const release = game.release_date || store?.release_date

  useEffect(() => {
    if (!game.content_id) {
      setAbout({ status: 'ready', game: null })
      return
    }
    const request = new AbortController()
    setAbout({ status: 'loading' })
    void api<GameAbout | null>(`/games/${game.id}/about`, { signal: request.signal }).then(data => {
      if (!request.signal.aborted) setAbout({ status: 'ready', game: data })
    }).catch(error => {
      if (!request.signal.aborted) setAbout({ status: 'error', message: String((error as Error).message) })
    })
    return () => request.abort()
  }, [game.id, game.content_id, aboutAttempt])

  async function removeGame() {
    setBusy(true)
    setError('')
    try {
      await api('/games/' + game.id, { method: 'DELETE' })
      removed()
    } catch (failure) {
      setError((failure as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return <>
    <button className="text-button back-link" onClick={back}><ArrowLeft size={17} /> К библиотеке</button>
    <section className="detail-hero">
      {game.background && <img className="hero-art" src={game.background} alt="" />}
      <div className="hero-gradient" />
      <div className="detail-title">
        <Cover game={game} />
        <div>
          <div className="detail-pills"><span className="platform">{game.platform}</span><span className="mono">{game.title_id}</span></div>
          <h1>{game.title}</h1>
          {game.edition && <p>{game.edition}</p>}
          <div className="detail-store-meta">
            {store?.genres.map(genre => <span className="store-genre" key={genre}>{genre}</span>)}
            {store?.publisher && <span>Издатель · <strong>{store.publisher}</strong></span>}
            {release && <span>Выход · <strong>{dateText(release)}</strong></span>}
            {store?.players && <span>Игроки · <strong>{store.players}</strong></span>}
          </div>
          <div className="detail-actions">
            <button className="button primary" onClick={edit}><Pencil size={16} /> Редактировать</button>
            <button className="button secondary" onClick={art}><ImagePlus size={17} /> Оформление</button>
            {game.content_id && <button className="button secondary" aria-haspopup="dialog" onClick={() => setShowAbout(true)}>
              <Info size={17} /> Об игре
            </button>}
          </div>
        </div>
      </div>
    </section>
    {showAbout && <AboutGameDialog about={about} close={() => setShowAbout(false)} retry={() => setAboutAttempt(value => value + 1)} />}
    <div className="detail-grid">
      <section className="panel">
        <h2>Игра в коллекции</h2>
        <dl className="meta-grid">
          <Meta name="Версия" className="mono">{game.version}</Meta>
          <Meta name="Требуемая прошивка" className="mono">{game.firmware}</Meta>
          <Meta name="Размер">{game.size_bytes != null ? sizeText(game.size_bytes) : unknown}</Meta>
          <Meta name="На диске">{fileLabel[game.file_state]}</Meta>
          <Meta name="Добавлена">{dateText(game.added_at)}</Meta>
        </dl>
      </section>
      <section className="panel">
        <div className="section-heading"><h2>Обновления</h2><button className="icon-button" aria-label="Проверить обновления этой игры" title="Проверить обновления этой игры"
          onClick={updates} disabled={updating}><RefreshCw size={17} /></button></div>
        <span className={'patch-status ' + game.patch.state}>{patchLabel[game.patch.state]}</span>
        <dl className="meta-grid">
          <Meta name="Последний официальный патч" className="mono">{game.patch.latest?.version}</Meta>
          <Meta name="Прошивка патча" className="mono" title={game.patch.latest?.firmware ?? undefined}>
            {patchFirmwareText(game.patch.latest?.firmware)}
          </Meta>
          <CompatiblePatch patch={game.patch} />
          <Meta name="Источник">{game.patch.latest?.source || game.patch.check?.source}</Meta>
        </dl>
        {game.patch.check?.message && <p className="muted note">{game.patch.check.message}</p>}
        <a href={patchArchive(game)} target="_blank" rel="noreferrer" className="text-button note">
          {(game.package_identity?.platform || game.platform) === 'PS5' ? 'ProsperoPatches' : 'OrbisPatches'} <ExternalLink size={13} />
        </a>
      </section>
      {game.dlc.length > 0 && <section className="panel span-two">
        <h2>Дополнения <span className="count-badge">{game.dlc.length}</span></h2>
        <div className="dlc-list">{game.dlc.map((item, i) => <div className="dlc-row" key={i}>
          <div className="dlc-icon"><Box size={19} /></div><div><strong>{item.name}</strong>{item.description && <p>{item.description}</p>}</div>
          <span className="status-pill">{dlcLabel[item.status]}</span>
        </div>)}</div>
      </section>}
      <section className="panel">
        <h2>Локализация</h2>
        <dl className="meta-grid">
          <Meta name="Русский язык">{localText(game.ru_text, game.ru_audio)}</Meta>
          <Meta name="Источник русификации">{game.localization_source}</Meta>
          <Meta name="Интерфейс и субтитры">{game.text_languages}</Meta>
          <Meta name="Озвучка">{game.audio_languages}</Meta>
        </dl>
      </section>
      <section className="panel"><h2>Примечания</h2><p className="notes">{game.notes || 'Пока пусто'}</p></section>
    </div>
    <button className="text-button remove-game" disabled={updating} onClick={() => setRemove(true)}>
      <Trash2 size={15} /> Удалить карточку
    </button>
    {remove && <Dialog title="Удалить карточку" close={() => { if (!busy) setRemove(false) }}>
      <div className="dialog-body">
        <p className="notes">{game.title}</p>
        <p className="muted">Из каталога будут удалены карточка и её примечания. Повторное сканирование может добавить игру заново.</p>
        {error && <p className="error" role="alert">{error}</p>}
      </div>
      <div className="dialog-footer">
        <button className="button outlined" disabled={busy} onClick={() => setRemove(false)}>Отмена</button>
        <button className="button primary" disabled={busy} onClick={() => void removeGame()}>Удалить из каталога</button>
      </div>
    </Dialog>}
  </>
}

export function ArtworkDialog({ game, close, save }: { game: Game; close: () => void; save: (game: Game) => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const [results, setResults] = useState<ArtCandidate[]>([])
  async function handle(task: () => Promise<void>) {
    setBusy(true)
    setError('')
    try { await task() }
    catch (failure) { setError((failure as Error).message) }
    finally { setBusy(false) }
  }
  const upload = (kind: string, file?: File) => {
    if (!file) return
    void handle(async () => {
      const form = new FormData()
      form.set('file', file)
      save(await api<Game>('/games/' + game.id + '/artwork?kind=' + kind, { method: 'POST', body: form }))
    })
  }
  return <Dialog title="Оформление карточки" close={busy ? () => {} : close} wide>
    <div className="dialog-body art-dialog">
      <div className="art-upload">
        <Cover game={game} />
        <div>
          <p className="muted">Текущая обложка · {game.cover_source || 'Нет изображения'}</p>
          <label className="field">Загрузить обложку<input type="file" accept="image/png,image/jpeg,image/webp" disabled={busy}
            onChange={event => upload('cover', event.target.files?.[0])} /></label>
          <label className="field">Загрузить фон<input type="file" accept="image/png,image/jpeg,image/webp" disabled={busy}
            onChange={event => upload('background', event.target.files?.[0])} /></label>
        </div>
      </div>
      <button className="button outlined" disabled={busy} onClick={() => void handle(async () => {
        const response = await api<{ items: ArtCandidate[]; issues: string[]; sources: Record<string, boolean> }>(
          '/games/' + game.id + '/artwork/search', { method: 'POST' })
        setResults(response.items)
        setInfo(response.issues.join('. ') || (response.items.length ? '' :
          Object.values(response.sources).some(Boolean) ? 'Варианты не найдены.' : 'IGDB и SteamGridDB можно подключить через .env.'))
      })}><Download size={16} /> {busy ? 'Подождите…' : 'Найти варианты онлайн'}</button>
      {info && <p className="muted note" role="status">{info}</p>}
      {error && <p className="error" role="alert">{error}</p>}
      {results.length > 0 && <div className="art-results">{results.map(candidate => <button key={candidate.token} disabled={busy}
        onClick={() => void handle(async () => save(await api<Game>('/games/' + game.id + '/artwork/select',
          json('POST', { token: candidate.token }))))}>
        <img src={candidate.url} alt="" /><span>{candidate.title}</span><small>{candidate.source} · {candidate.kind === 'cover' ? 'Обложка' : 'Фон'}</small>
      </button>)}</div>}
    </div>
  </Dialog>
}
