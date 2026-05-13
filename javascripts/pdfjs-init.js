/* PDF.js renderer for inline attachment views.
 *
 * Each per-attachment page emits a placeholder of the form
 *
 *   <div class="pdfjs-rendered" data-pdf-src="../foo.pdf">
 *     <noscript><iframe …/></noscript>
 *   </div>
 *
 * This script finds every such container on each Material navigation
 * tick, dynamically imports PDF.js from the jsdelivr CDN as an ES
 * module, and for each page renders a <canvas> (visible glyphs as
 * bitmap) plus a transparent <div class="textLayer"> (for selection
 * and Cmd+F). Pixels in a canvas are immune to iOS Safari's Page Zoom
 * text-only inflation, which is what broke the pdf2htmlEX-rendered
 * version on iPad — the visible page stays correct even when the
 * user's per-site Page Zoom is set above 100%.
 *
 * If PDF.js fails to load or the fetch errors, we fall back to a
 * native <iframe> so the page is still usable.
 */
(function () {
  var PDFJS_VERSION = "4.10.38";
  var PDFJS_BASE =
    "https://cdn.jsdelivr.net/npm/pdfjs-dist@" + PDFJS_VERSION + "/build";

  /* Dynamic import is gated behind a one-shot promise so PDF.js's
   * worker config only runs once even when multiple containers ask
   * for it concurrently. */
  var pdfjsLibPromise = null;
  function getPdfjsLib() {
    if (pdfjsLibPromise) return pdfjsLibPromise;
    pdfjsLibPromise = import(PDFJS_BASE + "/pdf.min.mjs").then(function (mod) {
      mod.GlobalWorkerOptions.workerSrc = PDFJS_BASE + "/pdf.worker.min.mjs";
      return mod;
    });
    return pdfjsLibPromise;
  }

  function fallbackToIframe(container) {
    var src = container.getAttribute("data-pdf-src");
    if (!src) return;
    var title = container.getAttribute("data-pdf-title") || "PDF";
    container.innerHTML = "";
    var iframe = document.createElement("iframe");
    iframe.src = src;
    iframe.title = title;
    iframe.style.width = "100%";
    iframe.style.height = "80vh";
    iframe.style.border = "0";
    container.appendChild(iframe);
  }

  /* Render a single PDF page into the container.
   *
   * Picks the render scale by dividing the container's content width
   * by the page's natural CSS-pixel width at scale 1. We then bake the
   * device pixel ratio into the canvas backing store (via a transform
   * passed to renderContext.transform) so retina screens get crisp
   * glyphs without inflating the canvas's CSS size. */
  function renderPage(pdfjsLib, pdf, pageNumber, container, targetWidth) {
    return pdf.getPage(pageNumber).then(function (page) {
      var baseViewport = page.getViewport({ scale: 1 });
      var scale = targetWidth / baseViewport.width;
      var viewport = page.getViewport({ scale: scale });
      var outputScale = window.devicePixelRatio || 1;

      var pageWrap = document.createElement("div");
      pageWrap.className = "pdfjs-page";
      pageWrap.style.width = Math.floor(viewport.width) + "px";
      pageWrap.style.height = Math.floor(viewport.height) + "px";

      var canvas = document.createElement("canvas");
      canvas.width = Math.floor(viewport.width * outputScale);
      canvas.height = Math.floor(viewport.height * outputScale);
      canvas.style.width = Math.floor(viewport.width) + "px";
      canvas.style.height = Math.floor(viewport.height) + "px";

      var ctx = canvas.getContext("2d");
      var transform =
        outputScale !== 1
          ? [outputScale, 0, 0, outputScale, 0, 0]
          : null;

      var textLayerDiv = document.createElement("div");
      textLayerDiv.className = "textLayer";

      pageWrap.appendChild(canvas);
      pageWrap.appendChild(textLayerDiv);
      container.appendChild(pageWrap);

      var renderTask = page.render({
        canvasContext: ctx,
        viewport: viewport,
        transform: transform,
      });

      return renderTask.promise.then(function () {
        /* The text layer needs the same CSS-pixel size and the same
         * --scale-factor as the canvas so its glyph spans line up with
         * the rasterized output. */
        textLayerDiv.style.setProperty("--scale-factor", String(scale));
        return page.getTextContent().then(function (textContent) {
          var textLayer = new pdfjsLib.TextLayer({
            textContentSource: textContent,
            container: textLayerDiv,
            viewport: viewport,
          });
          return textLayer.render();
        });
      });
    });
  }

  function renderContainer(container) {
    if (container.dataset.pdfjsRendered === "1") return;
    if (container.dataset.pdfjsRendering === "1") return;
    var src = container.getAttribute("data-pdf-src");
    if (!src) return;

    container.dataset.pdfjsRendering = "1";

    /* Pick the target render width from the container's content box.
     * If the container hasn't been laid out yet (e.g. inside a hidden
     * tab), bail out — fitPdfRendersToWidth or the next document$
     * tick will retry. */
    var rect = container.getBoundingClientRect();
    var targetWidth = rect.width;
    if (!targetWidth) {
      delete container.dataset.pdfjsRendering;
      return;
    }

    /* Drop the <noscript> fallback contents before we start painting
     * our own children in. */
    while (container.firstChild) container.removeChild(container.firstChild);

    getPdfjsLib()
      .then(function (pdfjsLib) {
        return pdfjsLib
          .getDocument({ url: src })
          .promise.then(function (pdf) {
            var chain = Promise.resolve();
            for (var i = 1; i <= pdf.numPages; i++) {
              (function (pageNum) {
                chain = chain.then(function () {
                  return renderPage(
                    pdfjsLib,
                    pdf,
                    pageNum,
                    container,
                    targetWidth,
                  );
                });
              })(i);
            }
            return chain;
          });
      })
      .then(function () {
        container.dataset.pdfjsRendered = "1";
        delete container.dataset.pdfjsRendering;
      })
      .catch(function (err) {
        console.error("pdfjs-init: render failed for", src, err);
        delete container.dataset.pdfjsRendering;
        fallbackToIframe(container);
      });
  }

  function renderAll() {
    var containers = document.querySelectorAll(".pdfjs-rendered");
    for (var i = 0; i < containers.length; i++) {
      renderContainer(containers[i]);
    }
  }

  if (typeof document$ !== "undefined" && document$.subscribe) {
    document$.subscribe(function () {
      renderAll();
    });
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderAll);
  } else {
    renderAll();
  }
})();
