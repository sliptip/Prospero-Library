import { useEffect, useRef, useState } from 'react'
import type { ChangeEvent, FormEvent, ReactNode } from 'react'
import { ArrowLeft, Box, Download, ExternalLink, ImagePlus, Info, Pencil, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import { Cover } from './Library'
import { api, compatiblePatchText, dateText, dlcLabel, fileLabel, json, languageList, localText, patchArchive, patchFirmwareText, patchLabel, russianSupport, sizeText, unknown } from './model'
import type { ArtCandidate, CustomLocalization, Dlc, Game, GameAbout, LocalPackage, PatchInfo, Platform, StoreDlc, StoreLocalization } from './model'

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

const blankDlc: Dlc = { name: '', description: null, content_id: null, status: 'not_added' }
const blankLocalization: CustomLocalization = { source: null, interface: null, subtitles: null, audio: null }
const customLanguageFields = [
  ['interface', 'Языки интерфейса'], ['subtitles', 'Языки субтитров'], ['audio', 'Языки озвучки'],
] as const
const enumBool = (value?: boolean | null) => value === true ? 'yes' : value === false ? 'no' : ''
const optionalBool = (value: string) => value === '' ? null : value === 'yes'
const languageOption = (value: boolean | null) => value === null ? 'Не указано' : value ? 'Авто · Есть' : 'Авто · Нет'

function storeDlcName(item: StoreDlc) {
  return item.kind === 'edition' ? 'Бонусы ' + item.name : item.name
}

function matchesStoreDlc(item: Dlc, candidate: StoreDlc) {
  if (candidate.content_id && item.content_id)
    return item.content_id === candidate.content_id || item.content_id === candidate.id
  const name = item.name.trim().toLocaleLowerCase()
  return name === storeDlcName(candidate).toLocaleLowerCase() ||
    (candidate.kind === 'edition' && name.replace(/ dlc$/, '') === candidate.name.toLocaleLowerCase())
}

export function GameEditor({ game, close, save }: { game?: Game; close: () => void; save: (game: Game) => void }) {
  const [platform, setPlatform] = useState<Platform>(game?.platform || 'PS5')
  const [editPackage, setEditPackage] = useState(false)
  const savedFields = {
    title: game?.title ?? '', title_id: game?.title_id ?? '', version: game?.version ?? '',
    firmware: game?.firmware ?? '', size_bytes: String(game?.size_bytes ?? ''),
  }
  const [fields, setFields] = useState(savedFields)
  const [dlc, setDlc] = useState<Dlc[]>(game?.dlc || [])
  const [storeDlc, setStoreDlc] = useState<StoreDlc[] | null>(null)
  const [storeError, setStoreError] = useState('')
  const [storeLoading, setStoreLoading] = useState(false)
  const [addingDlc, setAddingDlc] = useState<string | null>(null)
  const [custom, setCustom] = useState<CustomLocalization[]>(game?.custom_localizations || [])
  const [ruText, setRuText] = useState(enumBool(game?.ru_text))
  const [ruAudio, setRuAudio] = useState(enumBool(game?.ru_audio))
  const [localizationSource, setLocalizationSource] = useState(game?.localization_source || '')
  const store = game?.official_localization
  const automatic = russianSupport(store)
  const russian = russianSupport(store, custom, optionalBool(ruText), optionalBool(ruAudio))
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
  function updateLocalization(index: number, key: keyof CustomLocalization, value: string) {
    setCustom(groups => groups.map((group, i) => i === index ? { ...group, [key]: value } : group))
  }

  async function findDlc() {
    if (!game) return
    setStoreLoading(true)
    setStoreError('')
    try { setStoreDlc(await api<StoreDlc[]>(`/games/${game.id}/store-dlc`)) }
    catch (failure) { setStoreError((failure as Error).message) }
    finally { setStoreLoading(false) }
  }

  async function addStoreDlc(item: StoreDlc, refreshDescription = false) {
    setStoreError('')
    setAddingDlc(item.id)
    let description = item.description
    if (item.content_id && !description) {
      const url = '/store-dlc/' + encodeURIComponent(item.id) + '?locale=' + encodeURIComponent(item.locale)
      const details = await api<{ description: string | null }>(url).catch(() => null)
      description = details?.description ?? null
    }
    if (refreshDescription && !description) {
      setStoreError('Не удалось загрузить описание из PlayStation Store')
      setAddingDlc(null)
      return
    }
    setDlc(items => {
      const existing = items.find(row => matchesStoreDlc(row, item))
      if (existing) return items.map(row => row === existing ? {
        ...row, description: item.kind === 'edition' || refreshDescription ? description : row.description || description,
        content_id: item.content_id || row.content_id,
      } : row)
      if (refreshDescription || items.length >= 100) return items
      return [...items, { ...blankDlc, name: storeDlcName(item), description, content_id: item.content_id }]
    })
    setAddingDlc(null)
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (addingDlc) return
    setError('')
    setBusy(true)
    const data = new FormData(event.currentTarget)
    const value = (key: string) => String(data.get(key) || '').trim() || null
    const body = {
      title: value('title'), platform: shownPlatform, title_id: value('title_id')?.toUpperCase(),
      edit_pkg_fields: game?.package_identity ? editPackage : undefined,
      version: value('version'), firmware: value('firmware'), edition: value('edition'),
      size_bytes: value('size_bytes') ? Number(value('size_bytes')) : null, release_date: value('release_date'),
      ru_text: optionalBool(ruText), ru_audio: optionalBool(ruAudio), text_languages: value('text_languages'),
      audio_languages: value('audio_languages'),
      localization_source: localizationSource.trim() || null,
      custom_localizations: custom,
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
        <label className="field">{shownPlatform === 'PS4' ? 'Версия основной игры' : 'Версия в коллекции'}
          <input {...input('version')} placeholder={shownPlatform === 'PS5' ? '01.000.000' : '01.00'} /></label>
        <label className="field">Требуемая прошивка игры<input {...input('firmware')} placeholder="13.40" /></label>
        <label className="field">Размер в байтах<input {...input('size_bytes')} type="number" min="1" step="1" /></label>
        <span />
        {([
          ['Русский текст', ruText, setRuText, automatic.ruText], ['Русская озвучка', ruAudio, setRuAudio, automatic.ruAudio],
        ] as const).map(([label, value, change, detected]) => <label key={label} className="field">{label}
          <select value={value} onChange={event => change(event.target.value)}>
            <option value="">{languageOption(detected)}</option><option value="yes">Есть</option><option value="no">Нет</option>
          </select>
        </label>)}
        <label className="field">Языки интерфейса и субтитров<input name="text_languages" maxLength={512}
          defaultValue={game?.text_languages || ''} placeholder={languageList(store?.screen)} /></label>
        <label className="field">Языки озвучки<input name="audio_languages" maxLength={512}
          defaultValue={game?.audio_languages || ''} placeholder={languageList(store?.audio)} /></label>
        {(russian.ruText || russian.ruAudio) && <label className="field span-two">Источник русификации
          <input maxLength={512} value={localizationSource} placeholder={russian.source || undefined}
            onChange={event => setLocalizationSource(event.target.value)} />
        </label>}
        <div className="span-two localization-editor">
          <div className="section-heading"><h3>Дополнительные локализации</h3>
            <button className="button outlined compact" type="button" onClick={() => setCustom([...custom, { ...blankLocalization }])}>
              <Plus size={15} /> Добавить локализацию
            </button>
          </div>
          {custom.map((group, index) => <div className="localization-form" key={index}>
            <div className="section-heading">
              <label className="field">Название или автор<input maxLength={512} value={group.source || ''}
                placeholder="Например, команда перевода" onChange={event => updateLocalization(index, 'source', event.target.value)} /></label>
              <button className="icon-button" type="button" aria-label="Удалить локализацию"
                onClick={() => setCustom(custom.filter((_, i) => i !== index))}><X size={18} /></button>
            </div>
            {customLanguageFields.map(([key, label]) => <label className="field" key={key}>{label}
              <input maxLength={512} value={group[key] || ''} placeholder="Через запятую"
                onChange={event => updateLocalization(index, key, event.target.value)} />
            </label>)}
          </div>)}
        </div>
        <label className="field span-two">Примечания<textarea name="notes" rows={3} maxLength={12000} defaultValue={game?.notes || ''} /></label>
        <div className="span-two dlc-editor">
          <div className="section-heading dlc-actions">
            <h3>Дополнения</h3>
            <div className="button-row">
              {game?.content_id && <button className="button outlined compact" type="button"
                onClick={() => storeDlc ? setStoreDlc(null) : void findDlc()} disabled={storeLoading || !!addingDlc}
                aria-expanded={storeDlc !== null}>
                {storeLoading ? 'Ищу…' : storeDlc ? 'Скрыть результаты' : 'Найти в магазине'}
              </button>}
              <button className="button outlined compact" type="button" onClick={() => setDlc([...dlc, { ...blankDlc }])}
                disabled={dlc.length >= 100 || !!addingDlc}><Plus size={15} /> Добавить DLC</button>
            </div>
          </div>
          {storeError && <p className="muted note" role="status">{storeError}</p>}
          {storeDlc?.length === 0 && <p className="muted note" role="status">Магазин не показал дополнений или состава изданий для этой игры.</p>}
          {!!storeDlc?.length && <ul className="store-dlc-list" aria-label="Результаты PlayStation Store">
            {storeDlc.map(item => {
              const existing = dlc.find(row => matchesStoreDlc(row, item))
              const replaceEdition = !!existing && item.kind === 'edition' && item.description !== existing.description
              const refreshDescription = !!existing?.content_id && item.kind === 'dlc' && !!item.content_id
              const canFill = !!existing && ((!existing.content_id && !!item.content_id) || replaceEdition)
              const action = refreshDescription ? 'Обновить описание' : replaceEdition ? 'Вставить состав'
                : canFill ? 'Заполнить' : existing ? 'В карточке' : 'Добавить'
              return <li key={item.kind + item.id}>
                <div><small className="muted">{item.kind === 'edition' ? 'Состав издания' : 'DLC'}</small>
                  <strong>{item.name}</strong>
                  {item.description && <p>{item.description}</p>}</div>
                <button type="button" className="text-button" aria-label={action + ' ' + item.name}
                  disabled={!!addingDlc || (existing ? !canFill && !refreshDescription : dlc.length >= 100)}
                  onClick={() => void addStoreDlc(item, refreshDescription)}>
                  {addingDlc === item.id ? 'Загружаю…' : action}
                </button>
              </li>
            })}
          </ul>}
          {dlc.map((item, index) => <div className="dlc-form" key={index}>
            <label className="field">Название DLC<input value={item.name} required maxLength={256}
              onChange={event => setDlc(dlc.map((other, i) => i === index ? { ...other, name: event.target.value } : other))} /></label>
            <label className="field">Статус DLC<select value={item.status} onChange={event => setDlc(dlc.map((other, i) =>
              i === index ? { ...other, status: event.target.value as Dlc['status'] } : other))}>
              {item.status === 'unknown' && <option value="unknown" disabled>Выбери статус</option>}
              {Object.entries(dlcLabel).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select></label>
            <button className="icon-button" type="button" title="Удалить строку DLC" aria-label="Удалить строку DLC"
              onClick={() => setDlc(dlc.filter((_, i) => i !== index))}><X size={18} /></button>
            <label className="field span-two">Описание<textarea rows={2} value={item.description || ''}
              onChange={event => setDlc(dlc.map((other, i) => i === index ? { ...other, description: event.target.value } : other))} /></label>
          </div>)}
        </div>
        {error && <p className="error span-two" role="alert">{error}</p>}
      </div>
      <div className="dialog-footer"><button type="button" disabled={busy} className="button outlined" onClick={close}>Отмена</button>
        <button className="button primary" disabled={busy || !!addingDlc}>{busy ? 'Сохраняю…' : 'Сохранить'}</button></div>
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

function PackageFile({ item }: { item: LocalPackage }) {
  return <p className="package-file" title={item.relpath}>
    <span>{item.relpath.split('/').at(-1)}</span>
    <span>{sizeText(item.size_bytes)} · {fileLabel[item.file_state]}</span>
  </p>
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
        </> : <p className="muted">Не удалось найти описание в PlayStation Store.</p>}
        {about.game?.url?.startsWith('https://store.playstation.com/') && <a href={about.game.url} target="_blank" rel="noreferrer" className="text-button note">
          PlayStation Store <ExternalLink size={13} />
        </a>}
      </>}
    </div>
  </Dialog>
}

export function GamePage({ game, back, edit, art, updates, updating, removed, onLocalization }: {
  game: Game; back: () => void; edit: () => void; art: () => void; updates: () => void; updating: boolean; removed: () => void;
  onLocalization: (id: number, contentId: string, localization: StoreLocalization) => void;
}) {
  const [remove, setRemove] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [showAbout, setShowAbout] = useState(false)
  const [about, setAbout] = useState<AboutState>({ status: 'loading' })
  const [aboutAttempt, setAboutAttempt] = useState(0)
  const store = about.status === 'ready' ? about.game : null
  const release = game.release_date || store?.release_date
  const languages = game.official_localization
  const russian = russianSupport(languages, game.custom_localizations, game.ru_text, game.ru_audio)
  const source = (russian.ruText || russian.ruAudio) && (game.localization_source || russian.source)
  const localPatches = game.local_packages.filter(item => item.kind === 'patch')
  const localDlc = game.local_packages.filter(item => item.kind === 'dlc')
  const listedDlc = game.dlc.map(item => ({
    title: item.name, description: item.description, label: item.status === 'unknown' ? null : dlcLabel[item.status],
    files: localDlc.filter(file => file.content_id && file.content_id === item.content_id),
  }))
  const unlistedDlc = localDlc.filter(file => !file.content_id || !game.dlc.some(item => item.content_id === file.content_id))
  const byContent = new Map<string, LocalPackage[]>()
  for (const file of unlistedDlc) {
    const key = file.content_id || file.relpath
    const copies = byContent.get(key)
    if (copies) copies.push(file)
    else byContent.set(key, [file])
  }
  const dlc = [...listedDlc, ...[...byContent.values()].map(files => ({
    title: files[0].title, description: null, label: 'PKG', files,
  }))]

  useEffect(() => {
    const { id, content_id: contentId } = game
    if (!contentId) {
      setAbout({ status: 'ready', game: null })
      return
    }
    const request = new AbortController()
    setAbout({ status: 'loading' })
    void api<GameAbout | null>(`/games/${id}/about`, { signal: request.signal }).then(data => {
      if (request.signal.aborted) return
      if (data?.localization) onLocalization(id, contentId, data.localization)
      setAbout({ status: 'ready', game: data })
    }).catch(error => {
      if (!request.signal.aborted) setAbout({ status: 'error', message: String((error as Error).message) })
    })
    return () => request.abort()
  }, [game.id, game.content_id, aboutAttempt, onLocalization])

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
          <Meta name={localPatches.length ? 'Версия основной игры' : 'Версия'} className="mono">{game.version}</Meta>
          {game.collection_version && game.collection_version !== game.version &&
            <Meta name="Версия с патчем в коллекции" className="mono">{game.collection_version}</Meta>}
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
      {localPatches.length > 0 && <section className="panel span-two">
        <h2>Локальные патчи</h2>
        <div className="dlc-list">{localPatches.map((item, i) => <div className="dlc-row" key={i}>
          <div className="dlc-icon"><RefreshCw size={17} /></div>
          <div><strong>{item.version ? 'Версия ' + item.version : 'Версия неизвестна'}</strong><PackageFile item={item} /></div>
          {item.firmware && <span className="status-pill">FW {patchFirmwareText(item.firmware)}</span>}
        </div>)}</div>
      </section>}
      {dlc.length > 0 && <section className="panel span-two">
        <h2>Дополнения <span className="count-badge">{dlc.length}</span></h2>
        <div className="dlc-list">{dlc.map((item, i) => <div className="dlc-row" key={i}>
          <div className="dlc-icon"><Box size={19} /></div>
          <div><strong>{item.title}</strong>{item.description && <p>{item.description}</p>}
            {item.files.map((file, index) => <PackageFile key={index} item={file} />)}</div>
          {item.label && <span className="status-pill">{item.label}</span>}
        </div>)}</div>
      </section>}
      <section className="panel">
        <h2>Локализация</h2>
        <dl className="meta-grid">
          <Meta name="Русский язык">{localText(russian.ruText, russian.ruAudio)}</Meta>
          {source && <Meta name="Источник русификации">{source}</Meta>}
          <Meta name="Интерфейс и субтитры">{game.text_languages || languageList(languages?.screen)}</Meta>
          <Meta name="Озвучка">{game.audio_languages || languageList(languages?.audio)}</Meta>
        </dl>
        {game.custom_localizations.map((group, index) => <div className="localization-group" key={index}>
          <h3>{group.source || 'Дополнительная локализация'}</h3>
          <dl className="meta-grid">{customLanguageFields.map(([key, label]) =>
            group[key] && <Meta key={key} name={label}>{group[key]}</Meta>)}</dl>
        </div>)}
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
          Object.values(response.sources).some(Boolean) ? 'Варианты не найдены.' : 'IGDB и SteamGridDB можно подключить в настройках.'))
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
