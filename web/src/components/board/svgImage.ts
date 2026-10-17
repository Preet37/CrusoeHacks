import { AssetRecordType, type Editor, type TLAssetId, type TLImageShape, type TLShapeId, createShapeId } from 'tldraw'

export function svgToDataUrl(svg: string): string {
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg)
}

export interface Box { x: number; y: number; w: number; h: number }

/**
 * Place (or replace, when `existing` is an image shape) an SVG as an image asset + image shape.
 * The SVG's natural size (natW x natH) is scaled to fit inside `box`, preserving aspect ratio.
 * When `fit` is false the natural size is used as-is.
 */
export function placeSvgImage(
  editor: Editor,
  svg: string,
  natW: number,
  natH: number,
  box: Box,
  opts: { name: string; existing?: TLShapeId; fit?: boolean } = { name: 'twin-image' },
): TLShapeId {
  natW = Math.max(1, natW); natH = Math.max(1, natH)
  const scale = opts.fit === false ? 1 : Math.min(box.w / natW, box.h / natH)
  const w = Math.max(8, natW * scale), h = Math.max(8, natH * scale)
  const assetId: TLAssetId = AssetRecordType.createId()
  editor.createAssets([
    {
      id: assetId,
      typeName: 'asset',
      type: 'image',
      props: { name: opts.name, src: svgToDataUrl(svg), w: natW, h: natH, mimeType: 'image/svg+xml', isAnimated: false },
      meta: {},
    },
  ])
  const existing = opts.existing ? editor.getShape<TLImageShape>(opts.existing) : undefined
  if (existing && existing.type === 'image') {
    const oldAsset = existing.props.assetId
    editor.updateShape<TLImageShape>({ id: existing.id, type: 'image', props: { assetId, w, h, url: '' }, opacity: 1 })
    if (oldAsset && oldAsset !== assetId) {
      try { editor.deleteAssets([oldAsset]) } catch { /* ignore */ }
    }
    return existing.id
  }
  if (existing) editor.deleteShape(existing.id)
  const id = opts.existing ?? createShapeId()
  editor.createShape<TLImageShape>({
    id,
    type: 'image',
    x: box.x,
    y: box.y,
    opacity: 1,
    props: { assetId, w, h, altText: opts.name },
  })
  return id
}
