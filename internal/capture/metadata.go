package capture

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/cdproto/runtime"
	"github.com/chromedp/chromedp"
)

const (
	MetadataTextContent    = "textContent"
	MetadataAttributeValue = "attributeValue"
	MaxMetadataBytes       = 1024
)

type MetadataRequest struct {
	Method    string
	Selector  string
	Attribute string
}

func readCaptureMetadata(ctx context.Context, req *MetadataRequest) (*string, error) {
	if req == nil {
		return nil, nil
	}
	// Keep browser exceptions and protocol messages out of capture logs.
	failed := func() error {
		if err := ctx.Err(); err != nil {
			return err
		}
		return errors.New("capture metadata evaluation failed")
	}
	tree, err := chromedp.Call(ctx, page.GetFrameTree, cdp.Empty{})
	if err != nil {
		return nil, failed()
	}
	world, err := chromedp.Call(ctx, page.CreateIsolatedWorld, page.CreateIsolatedWorldParams{
		FrameID: tree.FrameTree.Frame.ID, WorldName: "webcappi-capture-metadata",
	})
	if err != nil {
		return nil, failed()
	}
	encoded, _ := json.Marshal(req)
	limit, _ := json.Marshal(MaxMetadataBytes)
	yes := true
	read, err := chromedp.Call(ctx, runtime.CallFunctionOn, runtime.CallFunctionOnParams{
		FunctionDeclaration: readMetadataDOM,
		ExecutionContextID:  world.ExecutionContextID,
		Arguments:           []*runtime.CallArgument{{Value: encoded}, {Value: limit}},
		Silent:              &yes,
		ReturnByValue:       &yes,
	})
	if err != nil || read.ExceptionDetails != nil || read.Result == nil {
		return nil, failed()
	}
	var result struct {
		Value *string `json:"value"`
		Error string  `json:"error"`
	}
	if err := json.Unmarshal(read.Result.Value, &result); err != nil {
		return nil, failed()
	}
	switch result.Error {
	case "selector":
		return nil, errors.New("invalid capture metadata selector")
	case "noMatch":
		return nil, errors.New("capture metadata selector matched no elements")
	case "multipleMatches":
		return nil, errors.New("capture metadata selector matched multiple elements")
	case "attribute":
		return nil, errors.New("capture metadata attribute is missing")
	case "size":
		return nil, errors.New("capture metadata exceeds size limit")
	}
	if result.Value == nil {
		return nil, failed()
	}
	return result.Value, nil
}

const readMetadataDOM = `function ({Method, Selector, Attribute}, limit) {
 if (window !== window.top) return {};
 let matches;
 try {
   matches = document.querySelectorAll(Selector);
 } catch {
   return {error: 'selector'};
 }
 if (matches.length === 0) return {error: 'noMatch'};
 if (matches.length !== 1) return {error: 'multipleMatches'};
 let value;
 switch (Method) {
   case 'textContent': value = matches[0].textContent; break;
   case 'attributeValue': value = matches[0].getAttribute(Attribute); break;
   default: return {};
 }
 if (value === null) return {error: 'attribute'};
 if (value.length > limit) return {error: 'size'};
 value = value.toWellFormed();
 if (new TextEncoder().encode(value).length > limit) return {error: 'size'};
 return {value};
}`
