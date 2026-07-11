import {Composition} from "remotion"
import {OpenReelPromo} from "../components/promo/OpenReelPromo"

export const PROMO_FPS = 30
export const PROMO_DURATION_IN_FRAMES = 24 * PROMO_FPS
export const PROMO_WIDTH = 1920
export const PROMO_HEIGHT = 1080

export function PromoVideoRoot() {
  return (
    <Composition
      id="OpenReelPromo"
      component={OpenReelPromo}
      durationInFrames={PROMO_DURATION_IN_FRAMES}
      fps={PROMO_FPS}
      width={PROMO_WIDTH}
      height={PROMO_HEIGHT}
    />
  )
}
