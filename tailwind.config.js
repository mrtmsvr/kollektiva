/* A főoldal (index.html) stílusa: előre lefordított Tailwind (tw.css), nem a böngészőben fordított CDN-es változat –
   így nem akad meg az oldal (pl. a keringő hold) betöltéskor. Az index.html módosításakor a GitHub Action
   (.github/workflows/tailwind.yml) magától újrafordítja. Kézzel: npx tailwindcss@3 -i tw-input.css -o tw.css --minify */
module.exports = {
  content: ['./index.html', './poll-widget.js'],
  theme: {
    extend: {
      colors: {
        night: '#0E1024', vault: '#171A36', line: '#2A2D52', parch: '#ECE6D8', dusk: '#9492B3', brass: '#C9A45C', brassdk: '#A8853F'
      },
      fontFamily: {
        display: ['"Cormorant Garamond"', 'Georgia', 'serif'],
        sans: ['Manrope', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif']
      }
    }
  }
};
