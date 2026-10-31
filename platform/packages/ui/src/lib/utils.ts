import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

/**
 * tokens.css の @theme で足した名前付きの値を tailwind-merge に教える。
 * 知らない値（`rounded-control`）は別のクラスとして扱われ、呼び出し側の `rounded-none` 等で上書きできなくなるため。
 */
const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      borderRadius: ["control"],
    },
  },
});

/** Tailwind クラスを安全に結合する（shadcn/ui 標準ユーティリティ）。 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
