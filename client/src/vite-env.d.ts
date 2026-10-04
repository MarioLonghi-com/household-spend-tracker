/// <reference types="vite/client" />

// The reference Vite's own scaffold ships and this client never had. It pulls
// in the ambient declarations for the things Vite lets you import that are not
// TypeScript -- `*.css` among them, which `main.tsx` imports for its
// side-effect.
//
// TypeScript 5 did not ask. TypeScript 7 does: a side-effect import of a
// module with no declaration is TS2882, so `import "./styles.css"` failed the
// typecheck while building perfectly well. The stylesheet was never untyped by
// intent; the file that says so was just missing.
