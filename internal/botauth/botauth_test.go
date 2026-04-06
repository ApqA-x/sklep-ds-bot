package botauth

import (
	"testing"

	"github.com/bwmarrin/discordgo"
)

func TestParseUserIDs(t *testing.T) {
	ids := ParseUserIDs("<@123>, 456\n<@!789> 456")
	if len(ids) != 3 || ids[0] != "123" || ids[1] != "456" || ids[2] != "789" {
		t.Fatalf("ids = %#v", ids)
	}
}

func TestIsAllowlisted(t *testing.T) {
	interaction := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{Member: &discordgo.Member{User: &discordgo.User{ID: "123"}}}}
	if !IsAllowlisted(interaction, []string{"<@123>"}) {
		t.Fatal("expected mention-form allowlist to match")
	}

	fallback := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{User: &discordgo.User{ID: "456"}}}
	if !IsAllowlisted(fallback, []string{"456"}) {
		t.Fatal("expected interaction user fallback to match")
	}

	if IsAllowlisted(interaction, []string{"999"}) {
		t.Fatal("expected unrelated user to fail")
	}
}
