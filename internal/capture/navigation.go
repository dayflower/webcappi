package capture

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/cdproto/fetch"
	"github.com/chromedp/cdproto/network"
	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/chromedp"
)

func documentHeaders(headers network.Headers) http.Header {
	result := make(http.Header)
	for key, value := range headers {
		if text, ok := value.(string); ok {
			for _, line := range strings.Split(text, "\n") {
				result.Add(key, line)
			}
		}
	}
	return result
}

// navigateDocument revalidates only the previously captured main document URL.
// Validators never reach unrelated redirects, child frames or subresources.
func navigateDocument(ctx context.Context, url string, previous *SourceResponse) (*SourceResponse, bool, error) {
	started := time.Now()
	if previous == nil || (previous.Headers.Get("ETag") == "" && previous.Headers.Get("Last-Modified") == "") {
		response, err := chromedp.Run(ctx, chromedp.NavigateResponse(url))
		return navigationSource(response, started, err)
	}
	tree, err := chromedp.Call(ctx, page.GetFrameTree, cdp.Empty{})
	if err != nil {
		return nil, false, err
	}
	navigation, cancel := context.WithCancel(ctx)
	defer cancel()
	events := cdp.Events(navigation, chromedp.FromContext(ctx).Target, fetch.RequestPaused)
	if _, err := chromedp.Call(ctx, fetch.Enable, fetch.EnableParams{Patterns: []*fetch.RequestPattern{{ResourceType: network.ResourceTypeDocument, RequestStage: fetch.RequestStageRequest}}}); err != nil {
		return nil, false, err
	}
	type validation struct {
		source *SourceResponse
		err    error
	}
	done := make(chan validation, 1)
	go func() {
		var result validation
		defer func() { done <- result }()
		for event, err := range events {
			if err != nil {
				return
			}
			main := event.FrameID == tree.FrameTree.Frame.ID && event.Request.URL == previous.URL
			if event.ResponseStatusCode == http.StatusNotModified && main {
				headers := make(http.Header)
				for _, header := range event.ResponseHeaders {
					headers.Add(header.Name, header.Value)
				}
				result.source = &SourceResponse{URL: previous.URL, Headers: headers, RequestStarted: started}
				// A 304 has no document to display. End navigation before readiness
				// checks and screenshot generation, then close the disposable browser.
				_, result.err = chromedp.Call(ctx, fetch.FailRequest, fetch.FailRequestParams{RequestID: event.RequestID, ErrorReason: network.ErrorReasonAborted})
				cancel()
				return
			}
			params := fetch.ContinueRequestParams{RequestID: event.RequestID}
			if event.ResponseStatusCode == 0 && main {
				for name, value := range event.Request.Headers {
					if strings.EqualFold(name, "If-None-Match") || strings.EqualFold(name, "If-Modified-Since") {
						continue
					}
					if text, ok := value.(string); ok {
						params.Headers = append(params.Headers, &fetch.HeaderEntry{Name: name, Value: text})
					}
				}
				for _, name := range []string{"If-None-Match", "If-Modified-Since"} {
					key := "ETag"
					if name == "If-Modified-Since" {
						key = "Last-Modified"
					}
					if value := previous.Headers.Get(key); value != "" {
						params.Headers = append(params.Headers, &fetch.HeaderEntry{Name: name, Value: value})
					}
				}
				yes := true
				params.InterceptResponse = &yes
			}
			if _, result.err = chromedp.Call(ctx, fetch.ContinueRequest, params); result.err != nil {
				cancel()
				return
			}
		}
	}()
	response, navigateErr := chromedp.Run(navigation, chromedp.NavigateResponse(url))
	cancel()
	validated := <-done
	if ctx.Err() != nil {
		return nil, false, ctx.Err()
	}
	if validated.err != nil {
		return nil, false, validated.err
	}
	if validated.source != nil {
		return validated.source, true, nil
	}
	if _, err := chromedp.Call(ctx, fetch.Disable, cdp.Empty{}); err != nil {
		return nil, false, err
	}
	return navigationSource(response, started, navigateErr)
}

func navigationSource(response *network.Response, started time.Time, err error) (*SourceResponse, bool, error) {
	if err != nil {
		return nil, false, err
	}
	if response == nil {
		return nil, false, nil
	}
	if response.Status >= 400 || response.Status == http.StatusNotModified {
		return nil, false, fmt.Errorf("page returned HTTP %d", response.Status)
	}
	return &SourceResponse{URL: response.URL, Headers: documentHeaders(response.Headers), RequestStarted: started}, false, nil
}
