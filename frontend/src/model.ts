export type Platform = 'PS4' | 'PS5'
export type DlcStatus = 'included' | 'not_added' | 'unknown'
export type Dlc = { name: string; description: string | null; content_id: string | null; status: DlcStatus }
export type StoreDlc = Omit<Dlc, 'status'> & { id: string; kind: 'dlc' | 'edition'; locale: string }
export type LocalPackage = {
  kind: 'patch' | 'dlc'; title: string; version: string | null; firmware: string | null;
  size_bytes: number; content_id: string | null; relpath: string; file_state: 'online' | 'offline';
}
export type StoreLocalization = { screen: string[] | null; audio: string[] | null }
export type CustomLocalization = { source: string | null; interface: string | null; subtitles: string | null; audio: string | null }
export type Patch = { version: string; firmware: string | null; source: string; checked_at: string; size_bytes: number | null }
export type PatchState = 'unknown' | 'available' | 'current' | 'error'
export type PatchInfo = {
  state: PatchState; latest: Patch | null; manual: Patch | null; compatible: Patch | null;
  target: string | null; history: Patch[]; cached: boolean; manual_active: boolean;
  check: { state: string; message: string | null; checked_at: string; last_success: string | null; source: string } | null;
}
export type Game = {
  id: number; title: string; platform: Platform; title_id: string; edition: string | null;
  version: string | null; collection_version: string | null; firmware: string | null; size_bytes: number | null;
  release_date: string | null; store_release_date: string | null;
  added_at: string; ru_text: boolean | null; ru_audio: boolean | null;
  text_languages: string | null; audio_languages: string | null; localization_source: string | null;
  official_localization: StoreLocalization | null; custom_localizations: CustomLocalization[];
  notes: string | null; manifest_uri: string | null; version_uri: string | null; dlc: Dlc[]; patch: PatchInfo; cover: string | null; background: string | null;
  cover_source: string | null; background_source: string | null; file_state: 'online' | 'offline' | 'unlinked';
  package_fields: string[]; package_identity: { platform: Platform; title_id: string } | null; content_id: string | null;
  local_packages: LocalPackage[];
}
export type GameAbout = {
  description: string | null; language: 'ru' | 'en'; publisher: string | null; genres: string[];
  release_date: string | null; players: string | null; url: string;
  localization?: StoreLocalization;
}
export type Root = { id: number; path: string; state: 'offline' | 'online' | 'unknown' | 'partial' }
export type Settings = { cli_path: string; cli_local: boolean; hide_recent: boolean; ps4_firmware: string; ps5_firmware: string; roots: Root[]; providers: Record<string, boolean> }
export type Job = {
  id: number; kind: 'scan' | 'updates'; status: 'running' | 'cancelling' | 'done' | 'cancelled' | 'error' | 'interrupted';
  report: { current?: string; found: number; added: number; updated: number; skipped: number; errors: number; issues: string[] };
}
export type ArtCandidate = { token: string; id: string; title: string; source: string; kind: 'cover' | 'background'; url: string; exact: boolean }

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch('/api' + path, options)
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = data?.detail
    const validation = Array.isArray(detail) ? detail.map(d => String(d.loc?.at(-1)) + ' — ' + d.msg).join('. ') : null
    throw new Error(typeof detail === 'string' ? detail : validation || 'Запрос не выполнен (HTTP ' + response.status + ')')
  }
  return data as T
}

export function json(method: string, body: unknown): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

export const unknown = 'Неизвестно'
export const patchLabel = { unknown: 'Нет данных', available: 'Есть обновление', current: 'Актуальная версия', error: 'Ошибка проверки' }
export const dlcLabel = { included: 'В образе', not_added: 'Не добавлено' }
export const fileLabel = { online: 'Файл доступен', offline: 'Файл недоступен', unlinked: 'Файл не привязан' }

export function availableDlcCount(game: Game) {
  const known = new Set<string>()
  let unnamed = 0
  for (const item of game.dlc) {
    if (item.status !== 'included') continue
    if (item.content_id) known.add('id:' + item.content_id)
    else unnamed += 1
  }
  for (const file of game.local_packages) {
    if (file.kind !== 'dlc' || file.file_state !== 'online') continue
    known.add(file.content_id ? 'id:' + file.content_id : 'path:' + file.relpath)
  }
  return known.size + unnamed
}

export function patchFirmwareText(firmware?: string | null) {
  return firmware?.split('.', 2).join('.') || unknown
}

export function compatiblePatchText(patch: PatchInfo) {
  if (patch.compatible) return patch.compatible.version + (patch.compatible.source === patch.manual?.source ? ' · Вручную' : '')
  if (patch.target && patch.latest && patch.history.every(item => item.firmware)) {
    return 'Нет в полученном списке'
  }
  return null
}

export function patchCacheNote(patch: PatchInfo) {
  if (!patch.cached) return null
  if (patch.manual_active) return 'Официальные сведения сохранены с предыдущей проверки'
  return patch.check?.last_success
    ? 'По данным проверки от ' + dateText(patch.check.last_success)
    : 'По сохранённым данным'
}

export function gameCount(count: number) {
  const tens = count % 100
  const ones = count % 10
  return count + ' ' + (tens >= 11 && tens <= 14 ? 'игр' : ones === 1 ? 'игра' : ones >= 2 && ones <= 4 ? 'игры' : 'игр')
}

export function sizeText(size: number | null) {
  if (size === null) return '—'
  return new Intl.NumberFormat('ru', { maximumFractionDigits: 2 }).format(size / 1024 ** 3) + ' ГиБ'
}

export function dateText(value?: string | null) {
  return value ? new Intl.DateTimeFormat('ru', { day: 'numeric', month: 'long', year: 'numeric' })
    .format(new Date(value.length === 10 ? value + 'T12:00:00' : value)) : unknown
}

export function localText(text: boolean | null, audio: boolean | null) {
  if (text && audio) return 'Полная'
  if (text) return 'Текстовая'
  if (audio) return 'Русская озвучка'
  if (text === false && audio === false) return 'Без локализации'
  return 'Не указано'
}

const languageNames = new Intl.DisplayNames(['ru'], { type: 'language', languageDisplay: 'standard' })

export function languageList(codes?: string[] | null) {
  return codes?.map(code => {
    let name = code
    try { name = languageNames.of(code) || code } catch { /* Keep the original code. */ }
    return name.charAt(0).toLocaleUpperCase('ru') + name.slice(1)
  }).join(', ') || ''
}

function storeRussian(codes?: string[] | null) {
  return codes?.length ? codes.some(code => code.split('-')[0].toLowerCase() === 'ru') : null
}

function customRussian(languages: string | null) {
  return languages?.split(/[,;/]/).some(language =>
    /^(?:ru(?:[-_]ru)?|russian|русск(?:ий|ая|ое|ие))(?=$|[^\p{L}\p{N}_-])/iu.test(language.trim()))
}

export function russianSupport(
  store?: StoreLocalization | null, groups: CustomLocalization[] = [], text?: boolean | null, audio?: boolean | null,
) {
  const storeText = storeRussian(store?.screen)
  const storeAudio = storeRussian(store?.audio)
  const ruText = groups.some(group => customRussian(group.interface) || customRussian(group.subtitles)) || (text ?? storeText)
  const ruAudio = groups.some(group => customRussian(group.audio)) || (audio ?? storeAudio)
  const officialText = text !== false && storeText
  const officialAudio = audio !== false && storeAudio
  const source = officialText && ruAudio && !officialAudio ? 'Официальная (текст)'
    : officialAudio && ruText && !officialText ? 'Официальная (озвучка)'
    : officialText || officialAudio ? 'Официальная' : null
  return { ruText, ruAudio, source }
}

export function stored<T>(key: string, fallback: T): T {
  try { return JSON.parse(localStorage.getItem(key) || 'null') ?? fallback }
  catch { return fallback }
}

export function patchArchive(game: Game) {
  const identity = game.package_identity || game
  return identity.platform === 'PS5' ? 'https://prosperopatches.com/' + identity.title_id
    : 'https://orbispatches.com/' + identity.title_id
}
